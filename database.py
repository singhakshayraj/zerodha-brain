import json
import uuid
from datetime import datetime, timezone
from supabase import create_client, Client
import config

supabase: Client = create_client(
    config.SUPABASE_URL,
    config.SUPABASE_SERVICE_KEY
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# --- CONFIG ---

def get_config(key: str):
    try:
        res = supabase.table('app_config').select('value').eq('key', key).limit(1).execute()
        if res.data and len(res.data) > 0:
            val = res.data[0].get('value')
            print(f"[database.get_config] key={key} found")
            return val
        print(f"[database.get_config] key={key} not found")
        return None
    except Exception as e:
        print(f"[database.get_config] error key={key}: {type(e).__name__}: {e}")
        return None


def write_config(key: str, value: str) -> None:
    try:
        payload = {
            'key': key,
            'value': value,
            'updated_at': _now_iso(),
        }
        supabase.table('app_config').upsert(payload).execute()
        print(f"[database.write_config] OK key={key}")
    except Exception as e:
        print(f"[database.write_config] error key={key}: {type(e).__name__}: {e}")
        print(f"[database.write_config] payload: {{'key': '{key}', 'value': '{value}'}}")


def get_enc_token():
    return get_config('enc_token')


def get_brain_command() -> str:
    result = get_config('brain_status')
    return result if result else 'IDLE'


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
}


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
        closed = [t for t in trades if t.get('status') == 'CLOSED']
        total_trades_executed = len(closed)
        winning_trades = sum(1 for t in closed if t.get('is_winner'))
        losing_trades = total_trades_executed - winning_trades
        total_pnl = sum((t.get('pnl') or 0) for t in closed)
        total_pnl_percent = sum((t.get('pnl_percent') or 0) for t in closed)

        print(f"[database.end_session] trades={total_trades_executed} wins={winning_trades} pnl={total_pnl:.2f}")

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


def get_open_trades(session_id: str) -> list:
    try:
        res = supabase.table('trades').select('*').eq('session_id', session_id).eq('status', 'OPEN').execute()
        return res.data or []
    except Exception as e:
        print(f"[database.get_open_trades] error: {e}")
        return []


def get_session_trades(session_id: str) -> list:
    try:
        res = supabase.table('trades').select('*').eq('session_id', session_id).order('created_at', desc=True).execute()
        return res.data or []
    except Exception as e:
        print(f"[database.get_session_trades] error: {e}")
        return []


# --- DECISIONS ---

def log_decision(session_id: str, decision_data: dict) -> None:
    try:
        payload = dict(decision_data)
        payload['session_id'] = session_id
        symbol = payload.get('symbol', '?')
        signal = payload.get('signal', '?')
        conf = payload.get('confidence_score', '?')
        print(f"[database.log_decision] {symbol} {signal} conf={conf}")
        supabase.table('brain_decisions').insert(payload).execute()
    except Exception as e:
        print(f"[database.log_decision] error: {type(e).__name__}: {e}")
        print(f"[database.log_decision] symbol={payload.get('symbol')}")


# --- BRAIN ACTIVITY ---

def log_brain_activity(
    session_id: str,
    activity_type: str,
    symbol: str = None,
    message: str = None,
    data: dict = None,
) -> None:
    try:
        payload = {
            'session_id': session_id,
            'activity_type': activity_type,
            'symbol': symbol,
            'message': message,
            'data': json.dumps(data) if data else None,
            'created_at': datetime.now(timezone.utc).isoformat(),
        }
        supabase.table('brain_activity').insert(payload).execute()
    except Exception as e:
        print(f"[DB] log_brain_activity error: {type(e).__name__}: {e}")


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
