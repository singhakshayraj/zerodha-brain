import hashlib
import json
import uuid
from datetime import datetime, timezone
from supabase import create_client, Client
import config

if not config.SUPABASE_URL or not config.SUPABASE_SERVICE_KEY:
    raise RuntimeError(
        "SUPABASE_URL and SUPABASE_SERVICE_KEY must be set in environment. "
        "Check Railway environment variables."
    )

supabase: Client = create_client(
    config.SUPABASE_URL,
    config.SUPABASE_SERVICE_KEY
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# --- ERROR BUDGET ---
# Consecutive control-plane failures. The heartbeat thread reports DEGRADED
# past the threshold so the watchdog can alert on partial DB failures that
# don't stop the heartbeat itself (errors otherwise only reach the logs).

_consecutive_failures = 0
DEGRADED_THRESHOLD = 5


def _record_failure() -> None:
    global _consecutive_failures
    _consecutive_failures += 1


def _record_success() -> None:
    global _consecutive_failures
    _consecutive_failures = 0


def health_degraded() -> bool:
    return _consecutive_failures >= DEGRADED_THRESHOLD


# --- CONFIG ---

def get_config(key: str):
    try:
        return get_config_strict(key)
    except Exception as e:
        print(f"[database.get_config] error key={key}: {type(e).__name__}: {e}")
        return None


def get_config_strict(key: str):
    """Like get_config but RAISES on query failure instead of returning None.

    get_config collapses "key missing/empty" and "query failed" into the same
    None — callers that treat None as a state change (e.g. the scheduler's
    active_session_id re-verification) would end a healthy session on a single
    transient Supabase error. Use this where that distinction matters."""
    try:
        res = supabase.table('app_config').select('value').eq('key', key).limit(1).execute()
    except Exception:
        _record_failure()
        raise
    _record_success()
    if res.data and len(res.data) > 0:
        val = res.data[0].get('value')
        print(f"[database.get_config] key={key} found")
        return val
    print(f"[database.get_config] key={key} not found")
    return None


def write_config(key: str, value: str) -> None:
    # Retried: these keys ARE the control plane (brain_status,
    # active_session_id). A silently dropped write during teardown leaves a
    # half-ended session the dashboard still sees as running.
    for attempt in range(3):
        try:
            payload = {
                'key': key,
                'value': value,
                'updated_at': _now_iso(),
            }
            supabase.table('app_config').upsert(payload).execute()
            _record_success()
            print(f"[database.write_config] OK key={key}")
            return
        except Exception as e:
            _record_failure()
            print(
                f"[database.write_config] error key={key} "
                f"(attempt {attempt + 1}/3): {type(e).__name__}: {e}"
            )
            if attempt < 2:
                import time
                time.sleep(2 * (attempt + 1))
    print(f"[database.write_config] GAVE UP key={key} value={value[:100]!r}")


def get_enc_token():
    return get_config('enc_token')


def get_brain_command() -> str:
    result = get_config('brain_status')
    return result if result else 'IDLE'


def has_session_today() -> bool:
    """True if any session was created today (IST trading day).
    Fails closed (True) so autopilot never double-starts on a DB error."""
    try:
        import pytz
        ist = pytz.timezone('Asia/Kolkata')
        now_ist = datetime.now(ist)
        day_start_utc = ist.localize(
            datetime(now_ist.year, now_ist.month, now_ist.day)
        ).astimezone(timezone.utc)
        res = (
            supabase.table('trading_sessions')
            .select('id')
            .gte('created_at', day_start_utc.isoformat())
            .limit(1)
            .execute()
        )
        return bool(res.data)
    except Exception as e:
        print(f"[database.has_session_today] error: {e}")
        return True


def get_session_config():
    raw = get_config('session_config')
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception as e:
        print(f"[database.get_session_config] parse error: {e}")
        return None


# --- HEARTBEAT ---

def update_heartbeat(status: str, cycle: int, message: str) -> None:
    try:
        payload = {
            'id': 1,
            'last_ping': _now_iso(),
            'status': status,
            'current_cycle': cycle,
            'brain_version': config.BRAIN_VERSION,
            'message': message,
        }
        supabase.table('brain_heartbeat').upsert(payload).execute()
        print(f"[database.update_heartbeat] {status} cycle={cycle}")
    except Exception as e:
        print(f"[database.update_heartbeat] error: {type(e).__name__}: {e}")


# --- SESSIONS ---

ALLOWED_SESSION_COLUMNS = {
    'capital_deployed',
    'max_trades',
    'max_loss_percent',
    'max_profit_percent',
    'trade_interval_seconds',
    'stock_universe',
    'status',
    'config_hash',
    'git_sha',
}


def get_session_by_id(session_id: str):
    try:
        res = (
            supabase.table('trading_sessions')
            .select('*')
            .eq('id', session_id)
            .limit(1)
            .execute()
        )
        return res.data[0] if res.data else None
    except Exception as e:
        print(f"[database.get_session_by_id] error: {e}")
        return None


def config_hash(session_config: dict) -> str:
    """Stable hash of the session's effective config (REQ-004): canonical
    JSON, sorted keys, 12 hex chars. Logged on the session row and every
    decision row so any mid-experiment tunable change is visible in data."""
    canonical = json.dumps(session_config, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()[:12]


def create_session(session_config: dict):
    try:
        print(f"[DB] Raw config received: {session_config}")

        # Build strictly — only the 7 known columns
        data = {
            'capital_deployed': float(session_config.get('capitalDeployed', 0)),
            'max_trades': int(session_config.get('maxTrades', 25)),
            'max_loss_percent': float(session_config.get('maxLossPercent', 5)),
            'max_profit_percent': float(session_config.get('maxProfitPercent', 15)),
            'trade_interval_seconds': int(session_config.get('tradeIntervalSeconds', 300)),
            'stock_universe': str(session_config.get('stockUniverse', 'BOTH')),
            'status': 'RUNNING',
        }

        # Hash the NORMALIZED tunables (not the raw payload) so a config
        # rebuilt from this row on resume hashes identically (REQ-004/031).
        data['config_hash'] = config_hash(
            {k: v for k, v in data.items() if k != 'status'}
        )
        data['git_sha'] = config.GIT_SHA

        # Defensive sanitize: drop any key not in whitelist
        data = {k: v for k, v in data.items() if k in ALLOWED_SESSION_COLUMNS}

        print(f"[DB] Inserting session data: {data}")
        print(f"[DB] Keys being sent: {list(data.keys())}")

        result = supabase.table('trading_sessions').insert(data).execute()

        print(f"[DB] Raw result: {result}")
        print(f"[DB] Result data: {result.data}")

        if result.data and len(result.data) > 0:
            session_id = result.data[0]['id']
            print(f"[DB] Session created successfully: {session_id}")
            return result.data[0]
        else:
            print(f"[DB] No data returned from insert")
            print(f"[DB] Result model: {result.model_dump() if hasattr(result, 'model_dump') else 'N/A'}")
            return None

    except Exception as e:
        print(f"[DB] Session creation FAILED")
        print(f"[DB] Error type: {type(e).__name__}")
        print(f"[DB] Error message: {str(e)}")
        import traceback
        traceback.print_exc()
        return None


def get_or_create_session(session_config: dict) -> str:
    session_id = get_config('active_session_id')
    if session_id:
        print(f"[database.get_or_create_session] using existing session: {session_id}")
        return session_id
    print("[database.get_or_create_session] no active session found, creating new")
    sess = create_session(session_config)
    if sess:
        session_id = sess.get('id')
        write_config('active_session_id', session_id)
        print(f"[database.get_or_create_session] created and stored: {session_id}")
        return session_id
    print("[database.get_or_create_session] failed to create session")
    return None


def update_session(session_id: str, updates: dict) -> None:
    try:
        print(f"[database.update_session] session={session_id}, updates={updates}")
        supabase.table('trading_sessions').update(updates).eq('id', session_id).execute()
        print(f"[database.update_session] OK")
    except Exception as e:
        print(f"[database.update_session] error session={session_id}: {type(e).__name__}: {e}")
        print(f"[database.update_session] updates: {updates}")


def end_session(session_id: str, end_reason: str) -> None:
    try:
        print(f"[database.end_session] closing session={session_id} reason={end_reason}")
        sess_res = supabase.table('trading_sessions').select('started_at').eq('id', session_id).limit(1).execute()
        duration_seconds = None
        if sess_res.data and len(sess_res.data) > 0:
            started_at_raw = sess_res.data[0].get('started_at')
            if started_at_raw:
                try:
                    started_at = datetime.fromisoformat(started_at_raw.replace('Z', '+00:00'))
                    duration_seconds = int((datetime.now(timezone.utc) - started_at).total_seconds())
                    print(f"[database.end_session] duration: {duration_seconds}s")
                except Exception as e:
                    print(f"[database.end_session] duration calc error: {type(e).__name__}: {e}")

        trades = get_session_trades(session_id)

        # Count executed: any trade with an entry_price was placed
        executed = [t for t in trades if t.get('entry_price') is not None]
        closed = [t for t in trades if t.get('status') == 'CLOSED']

        total_trades_executed = len(executed)
        winning_trades = sum(1 for t in closed if (t.get('pnl') or 0) > 0)
        losing_trades = sum(1 for t in closed if (t.get('pnl') or 0) <= 0)
        total_pnl = sum((t.get('pnl') or 0) for t in closed)
        # P&L as % of session capital — summing per-trade percentages (each
        # against its own position value) produced a meaningless number.
        cap_res = (
            supabase.table('trading_sessions')
            .select('capital_deployed')
            .eq('id', session_id)
            .limit(1)
            .execute()
        )
        capital = (cap_res.data[0].get('capital_deployed') or 0) if cap_res.data else 0
        total_pnl_percent = (total_pnl / capital * 100) if capital else 0

        # Fallback for trade COUNT only (not pnl)
        if total_trades_executed == 0:
            sess_full = (
                supabase.table('trading_sessions')
                .select('total_trades_executed,winning_trades')
                .eq('id', session_id)
                .limit(1)
                .execute()
            )
            if sess_full.data:
                row = sess_full.data[0]
                total_trades_executed = row.get('total_trades_executed') or 0
                winning_trades = row.get('winning_trades') or 0

        print(
            f"[database.end_session] trades={total_trades_executed} "
            f"wins={winning_trades} pnl={total_pnl:.2f}"
        )

        updates = {
            'status': 'COMPLETED',
            'ended_at': _now_iso(),
            'end_reason': end_reason,
            'total_trades_executed': total_trades_executed,
            'winning_trades': winning_trades,
            'losing_trades': losing_trades,
            'total_pnl': total_pnl,
            'total_pnl_percent': total_pnl_percent,
        }
        if duration_seconds is not None:
            updates['duration_seconds'] = duration_seconds

        supabase.table('trading_sessions').update(updates).eq('id', session_id).execute()
        print(f"[database.end_session] OK")
    except Exception as e:
        print(f"[database.end_session] error session={session_id}: {type(e).__name__}: {e}")


# --- TRADES ---

def create_trade(session_id: str, trade_data: dict):
    try:
        payload = dict(trade_data)
        payload['session_id'] = session_id
        if 'status' not in payload:
            payload['status'] = 'OPEN'
        print(f"[database.create_trade] inserting: {payload}")
        res = supabase.table('trades').insert(payload).execute()
        if res.data and len(res.data) > 0:
            trade_id = res.data[0].get('id')
            print(f"[database.create_trade] OK trade_id={trade_id}")
            return res.data[0]
        print(f"[database.create_trade] no data in response")
        return None
    except Exception as e:
        print(f"[database.create_trade] error: {type(e).__name__}: {e}")
        print(f"[database.create_trade] payload: {payload}")
        return None


def update_trade_entry(trade_id: str, order_data: dict) -> None:
    try:
        payload = dict(order_data)
        payload['status'] = 'OPEN'
        entry_price = payload.get('entry_price', '?')
        qty = payload.get('quantity', '?')
        print(f"[database.update_trade_entry] trade={trade_id} entry_price={entry_price} qty={qty}")
        supabase.table('trades').update(payload).eq('id', trade_id).execute()
        print(f"[database.update_trade_entry] OK")
    except Exception as e:
        print(f"[database.update_trade_entry] error trade={trade_id}: {type(e).__name__}: {e}")


def close_trade(trade_id: str, exit_data: dict) -> None:
    try:
        payload = dict(exit_data)
        pnl = payload.get('pnl', 0) or 0
        reason = payload.get('exit_reason', '?')
        payload['is_winner'] = pnl > 0
        payload['status'] = 'CLOSED'
        print(f"[database.close_trade] trade={trade_id} pnl={pnl:.2f} reason={reason}")
        supabase.table('trades').update(payload).eq('id', trade_id).execute()
        print(f"[database.close_trade] OK")
    except Exception as e:
        print(f"[database.close_trade] error trade={trade_id}: {type(e).__name__}: {e}")


def cleanup_unfilled_trades(session_id: str) -> int:
    """Void OPEN trades in THIS session that never got a fill (entry_price
    NULL). Happens when the process dies between create_trade and the order
    result landing. Left alone, a resumed session hits them every cycle in
    _check_and_close_positions with quantity=None → TypeError → the whole
    cycle aborts forever while the heartbeat stays green."""
    try:
        res = (
            supabase.table('trades')
            .select('id,symbol')
            .eq('session_id', session_id)
            .eq('status', 'OPEN')
            .is_('entry_price', 'null')
            .execute()
        )
        unfilled = res.data or []
        if not unfilled:
            return 0
        print(f"[database.cleanup_unfilled_trades] voiding {len(unfilled)} unfilled OPEN trades")
        for t in unfilled:
            supabase.table('trades').update({
                'status': 'CLOSED',
                'exit_reason': 'UNFILLED_VOID',
                'pnl': 0,
                'pnl_percent': 0,
            }).eq('id', t['id']).execute()
        return len(unfilled)
    except Exception as e:
        print(f"[database.cleanup_unfilled_trades] error: {e}")
        return 0


def cleanup_stale_open_trades(current_session_id: str) -> int:
    """Mark any OPEN trades from prior (non-current) sessions as STALE_CLEANUP."""
    try:
        res = (
            supabase.table('trades')
            .select('id,session_id,symbol')
            .eq('status', 'OPEN')
            .neq('session_id', current_session_id)
            .execute()
        )
        stale = res.data or []
        if not stale:
            return 0
        print(f"[database.cleanup_stale_open_trades] {len(stale)} stale OPEN trades from prior sessions")
        for t in stale:
            supabase.table('trades').update({
                'status': 'CLOSED',
                'exit_reason': 'STALE_CLEANUP',
            }).eq('id', t['id']).execute()
        return len(stale)
    except Exception as e:
        print(f"[database.cleanup_stale_open_trades] error: {e}")
        return 0


def get_open_trades(session_id: str) -> list:
    try:
        res = supabase.table('trades').select('*').eq('session_id', session_id).eq('status', 'OPEN').execute()
        return res.data or []
    except Exception as e:
        print(f"[database.get_open_trades] error: {e}")
        return []


def get_open_shorts(session_id: str) -> list:
    """Return all open SHORT positions for the session."""
    try:
        res = (
            supabase.table('trades')
            .select('*')
            .eq('session_id', session_id)
            .eq('status', 'OPEN')
            .eq('position_type', 'SHORT')
            .execute()
        )
        return res.data or []
    except Exception as e:
        print(f"[database.get_open_shorts] error: {e}")
        return []


def get_open_longs(session_id: str) -> list:
    """Return all open LONG positions for the session."""
    try:
        res = (
            supabase.table('trades')
            .select('*')
            .eq('session_id', session_id)
            .eq('status', 'OPEN')
            .neq('position_type', 'SHORT')
            .execute()
        )
        return res.data or []
    except Exception as e:
        print(f"[database.get_open_longs] error: {e}")
        return []


def get_win_rate() -> tuple:
    """Lifetime win rate across all CLOSED trades with non-null pnl.
    Returns (win_rate, total_trades). Fallback 0.45 when <10 trades."""
    try:
        res = (
            supabase.table('trades')
            .select('pnl')
            .eq('status', 'CLOSED')
            .not_.is_('pnl', 'null')
            .execute()
        )
        trades = res.data or []
        total = len(trades)
        if total < 10:
            return 0.45, total
        wins = sum(1 for t in trades if (t.get('pnl') or 0) > 0)
        win_rate = wins / total
        print(f"[kelly] Historical win rate: {wins}/{total} = {win_rate:.1%}")
        return win_rate, total
    except Exception as e:
        print(f"[kelly] Error fetching win rate: {e}")
        return 0.45, 0


def get_session_trades(session_id: str) -> list:
    try:
        res = supabase.table('trades').select('*').eq('session_id', session_id).order('created_at', desc=True).execute()
        return res.data or []
    except Exception as e:
        print(f"[database.get_session_trades] error: {e}")
        return []


# --- DECISIONS ---

def log_decision(
    session_id: str,
    symbol: str,
    signal: str,
    confidence: int,
    indicators: dict,
    reasons: list,
    skip_reasons: list,
    live_price: float = 0,
    nifty_level: float = 0,
    time_bucket: str = 'NORMAL',
    stop_loss: float = None,
    target: float = None,
    risk_reward: float = None,
    position_size: int = None,
    regime: str = None,
    market_bias: str = None,
    **kwargs,
) -> None:
    """Log decision; extra fields merged into indicators JSONB."""
    try:
        enhanced = dict(indicators) if indicators else {}

        if stop_loss is not None:
            enhanced['stop_loss'] = float(stop_loss)
        if target is not None:
            enhanced['target'] = float(target)
        if risk_reward is not None:
            enhanced['risk_reward'] = float(risk_reward)
        if position_size is not None:
            enhanced['position_size'] = int(position_size)
        if regime is not None:
            enhanced['regime'] = str(regime)
        if market_bias is not None:
            enhanced['market_bias'] = str(market_bias)

        for k, v in kwargs.items():
            if v is not None:
                enhanced[k] = v

        payload = {
            'session_id': session_id,
            'symbol': symbol,
            'signal': signal,
            'confidence_score': int(confidence),
            'indicators': enhanced,
            'reasons': reasons if reasons else [],
            'skip_reasons': skip_reasons if skip_reasons else [],
            'price_at_decision': float(live_price) if live_price else 0,
            'nifty_level_at_decision': float(nifty_level) if nifty_level else 0,
            'time_of_day_bucket': time_bucket,
            'decided_at': datetime.now(timezone.utc).isoformat(),
        }

        supabase.table('brain_decisions').insert(payload).execute()
        print(f"[log_decision] OK {symbol} {signal} conf={confidence}")

    except Exception as e:
        print(f"[log_decision] error: {e}")
        print(f"[log_decision] symbol={symbol}")


def log_quote_snapshot(session_id: str, cycle: int, prices: dict) -> None:
    """One row per cycle: jsonb map of symbol -> LTP for the scanned
    universe, so training can reconstruct what the brain saw."""
    try:
        supabase.table('quote_snapshots').insert({
            'session_id': session_id,
            'cycle': int(cycle),
            'prices': prices,
        }).execute()
        print(f"[log_quote_snapshot] OK cycle={cycle} symbols={len(prices)}")
    except Exception as e:
        print(f"[log_quote_snapshot] error: {e}")


# --- BRAIN ACTIVITY ---

def log_brain_activity(
    session_id: str,
    activity_type: str,
    symbol: str = None,
    message: str = None,
    data: dict = None,
) -> None:
    """
    Log activity for live UI feed.

    activity_type: CYCLE_START | ANALYZING | SIGNAL |
                   ORDER_PLACED | ORDER_FAILED |
                   POSITION_EXIT | SESSION_END | ERROR
    """
    try:
        payload = {
            'session_id': session_id,
            'activity_type': activity_type,
            'created_at': datetime.now(timezone.utc).isoformat(),
        }
        if symbol is not None:
            payload['symbol'] = symbol
        if message is not None:
            payload['message'] = message
        if data is not None:
            payload['data'] = data

        supabase.table('brain_activity').insert(payload).execute()
        print(f"[activity] {activity_type} {symbol or ''} {message or ''}")
    except Exception as e:
        print(f"[log_brain_activity] error: {e}")


# --- MARKET CONTEXT ---

def log_market_context(session_id: str, context_data: dict) -> None:
    try:
        payload = dict(context_data)
        payload['session_id'] = session_id
        nifty = payload.get('nifty_level', '?')
        bucket = payload.get('time_bucket', '?')
        print(f"[database.log_market_context] nifty={nifty} bucket={bucket}")
        supabase.table('market_context').insert(payload).execute()
    except Exception as e:
        print(f"[database.log_market_context] error: {type(e).__name__}: {e}")


# --- STOCK UNIVERSE ---

def get_stock_universe(filter: str = 'ALL') -> list:
    try:
        q = supabase.table('stock_universe').select('*').eq('is_active', True)
        if filter == 'NIFTY50':
            q = q.eq('is_nifty50', True)
        elif filter == 'HOLDINGS':
            q = q.eq('is_nifty50', False)
        res = q.order('brain_score', desc=True).execute()
        return res.data or []
    except Exception as e:
        print(f"[database.get_stock_universe] error: {e}")
        return []


def get_top_scored_stocks(limit: int = 10) -> list:
    try:
        res = (
            supabase.table('stock_universe')
            .select('*')
            .eq('is_active', True)
            .order('brain_score', desc=True)
            .limit(limit)
            .execute()
        )
        return res.data or []
    except Exception as e:
        print(f"[database.get_top_scored_stocks] error: {e}")
        return []


def update_stock_score(symbol: str, is_winner: bool, pnl: float) -> None:
    try:
        res = supabase.table('stock_universe').select('*').eq('symbol', symbol).limit(1).execute()
        if not res.data or len(res.data) == 0:
            print(f"[database.update_stock_score] symbol not found: {symbol}")
            return
        row = res.data[0]

        total_trades = (row.get('total_trades') or 0) + 1
        winning_trades = row.get('winning_trades') or 0
        brain_score = row.get('brain_score') or 0
        total_pnl = (row.get('total_pnl') or 0) + pnl

        if is_winner:
            winning_trades += 1
            brain_score = min(100, brain_score + 2)
        else:
            brain_score = max(0, brain_score - 3)

        avg_pnl_per_trade = total_pnl / total_trades if total_trades > 0 else 0
        win_rate = (winning_trades / total_trades) * 100 if total_trades > 0 else 0

        now = _now_iso()
        updates = {
            'total_trades': total_trades,
            'winning_trades': winning_trades,
            'brain_score': brain_score,
            'total_pnl': total_pnl,
            'avg_pnl_per_trade': avg_pnl_per_trade,
            'win_rate': win_rate,
            'last_traded_at': now,
            'last_updated_at': now,
        }
        print(f"[database.update_stock_score] {symbol} trades={total_trades} score={brain_score:.0f}")
        supabase.table('stock_universe').update(updates).eq('symbol', symbol).execute()
        print(f"[database.update_stock_score] OK")
    except Exception as e:
        print(f"[database.update_stock_score] error {symbol}: {type(e).__name__}: {e}")


def add_holdings_to_universe(holdings: list) -> None:
    for h in holdings:
        try:
            symbol = h.get('tradingsymbol')
            exchange = h.get('exchange')
            if not symbol:
                continue
            existing = supabase.table('stock_universe').select('symbol').eq('symbol', symbol).limit(1).execute()
            if existing.data and len(existing.data) > 0:
                continue
            supabase.table('stock_universe').insert({
                'symbol': symbol,
                'exchange': exchange,
                'is_nifty50': False,
                'is_active': True,
            }).execute()
        except Exception as e:
            print(f"[database.add_holdings_to_universe] error for {h.get('tradingsymbol')}: {e}")
