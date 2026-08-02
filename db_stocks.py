"""Stock / observation / universe / level / advice-snapshot data access
(SE4 module split part 2, P-06). Extracted from database.py. These reference the
shared client + helpers back on the `database` module at call time
(`database.supabase`, `database._now_iso`) so tests that patch `database.supabase`
still intercept them, and database.py re-exports every name here so callers keep
using `db.<name>` unchanged.
"""
from datetime import datetime, timezone  # noqa: F401  (used by moved fns)

import database

def insert_stock_observation(row: dict) -> bool:
    """One per-stock timeline row (stock_agent.build_observation output).
    Non-fatal — a capture failure never blocks the advisory run."""
    try:
        database.supabase.table('stock_observations').insert(row).execute()
        return True
    except Exception as e:
        print(f"[insert_stock_observation] {row.get('symbol')}: {e}")
        return False


def get_recent_observations(symbol: str, limit: int = 24) -> list:
    """A symbol's most recent timeline rows, oldest-first (for summarize)."""
    try:
        res = (database.supabase.table('stock_observations').select('*')
               .eq('symbol', symbol)
               .order('observed_at', desc=True).limit(limit).execute())
        return list(reversed(res.data or []))
    except Exception as e:
        print(f"[get_recent_observations] {symbol}: {e}")
        return []


def stock_symbols_observed_since(ts_iso: str) -> set:
    """Symbols with at least one observation since ts_iso — the hourly-dedup
    filter so frequent intraday refreshes don't flood the timeline."""
    try:
        res = (database.supabase.table('stock_observations').select('symbol')
               .gte('observed_at', ts_iso).execute())
        return {r['symbol'] for r in (res.data or [])}
    except Exception as e:
        print(f"[stock_symbols_observed_since] error: {e}")
        return set()


def stock_symbols_observed_today_in_phase(ist_date: str, phase: str) -> set:
    """Symbols already captured TODAY in a specific phase — the dedup for the
    once-per-day PRE_OPEN/POST_CLOSE snapshots. Distinct from the hourly filter
    above: a POST_CLOSE (15:35) must NOT be deduped against a 15:2X intraday
    refresh, so this keys on (phase, today) instead of a rolling hour."""
    try:
        start_ist = f"{ist_date}T00:00:00+05:30"
        res = (database.supabase.table('stock_observations').select('symbol')
               .eq('phase', phase).gte('observed_at', start_ist).execute())
        return {r['symbol'] for r in (res.data or [])}
    except Exception as e:
        print(f"[stock_symbols_observed_today_in_phase] error: {e}")
        return set()


def write_official_portfolio_advice(rows: list) -> int:
    """The day's ONE canonical advisory batch (rotation scan + digest +
    backtest-eligible) — is_official=True on every row. Same-day re-run
    (manual force-trigger) replaces the prior official batch rather than
    upserting per-row, since there is no longer a (run_date, symbol) unique
    constraint (intraday snapshot rows share that key many times a day)."""
    if not rows:
        return 0
    run_date = rows[0].get('run_date')
    try:
        if run_date:
            (database.supabase.table('portfolio_advice').delete()
             .eq('run_date', run_date).eq('is_official', True).execute())
        database.supabase.table('portfolio_advice').insert(rows).execute()
        return len(rows)
    except Exception as e:
        print(f"[write_official_portfolio_advice] error ({len(rows)} rows): {e}")
        return 0


def insert_portfolio_advice_snapshot(rows: list) -> int:
    """One intraday refresh batch (is_official=False) — plain append, never
    overwrites. Each call is a new timestamped snapshot so the dataset
    accrues an intraday time series per holding."""
    if not rows:
        return 0
    try:
        database.supabase.table('portfolio_advice').insert(rows).execute()
        return len(rows)
    except Exception as e:
        print(f"[insert_portfolio_advice_snapshot] error ({len(rows)} rows): {e}")
        return 0


def get_official_advice_for_date(run_date: str) -> list:
    """Today's official advice rows, keyed by symbol by the caller — used by
    the intraday lite refresh to carry forward rotation targets without
    rescanning the Nifty 500 every 5 minutes."""
    try:
        res = (database.supabase.table('portfolio_advice').select('*')
               .eq('run_date', run_date).eq('is_official', True).execute())
        return res.data or []
    except Exception as e:
        print(f"[get_official_advice_for_date] error: {e}")
        return []


def has_official_advisor_run(run_date: str) -> bool:
    """Whether today's official (once-daily, backtest-eligible) advisor batch
    has already been written — the DB is the source of truth for this dedup,
    not an in-memory flag, so it survives a Railway redeploy mid-day."""
    try:
        res = (database.supabase.table('portfolio_advice').select('run_date')
               .eq('run_date', run_date).eq('is_official', True)
               .limit(1).execute())
        return bool(res.data)
    except Exception as e:
        print(f"[has_official_advisor_run] error: {e}")
        return False


def get_last_advisor_run_time(run_date: str):
    """Timestamp of the most recent advisor write today (official or
    intraday snapshot) — drives the intraday refresh interval gate. Returns
    None if nothing has run yet today."""
    try:
        res = (database.supabase.table('portfolio_advice').select('created_at')
               .eq('run_date', run_date)
               .order('created_at', desc=True).limit(1).execute())
        if res.data:
            return res.data[0]['created_at']
        return None
    except Exception as e:
        print(f"[get_last_advisor_run_time] error: {e}")
        return None


def recent_news_for_symbol(symbol: str, before_iso: str, limit: int = 3) -> list:
    """Latest news rows tagging `symbol`, published strictly BEFORE before_iso
    (causal — no leakage of news the decision couldn't have seen). Returns []
    on any error so a decision never breaks on the news path."""
    try:
        res = (database.supabase.table('news_events')
               .select('published_at, headline, sentiment_score, sentiment_label, url')
               .contains('symbols', [symbol])
               .lt('published_at', before_iso)
               .order('published_at', desc=True)
               .limit(limit)
               .execute())
        return res.data or []
    except Exception as e:
        print(f"[recent_news_for_symbol] error {symbol}: {e}")
        return []


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

        database.supabase.table('brain_activity').insert(payload).execute()
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
        database.supabase.table('market_context').insert(payload).execute()
    except Exception as e:
        print(f"[database.log_market_context] error: {type(e).__name__}: {e}")


# --- STOCK UNIVERSE ---

def get_stock_universe(filter: str = 'ALL') -> list:
    try:
        q = database.supabase.table('stock_universe').select('*').eq('is_active', True)
        if filter == 'NIFTY50':
            q = q.eq('is_nifty50', True)
        elif filter == 'HOLDINGS':
            q = q.eq('is_nifty50', False)
        elif filter == 'NIFTY500':
            q = q.eq('is_nifty500', True)
        res = q.order('brain_score', desc=True).execute()
        return res.data or []
    except Exception as e:
        print(f"[database.get_stock_universe] error: {e}")
        return []


def upsert_stock_universe_bulk(rows: list) -> int:
    """Bulk seed/refresh universe rows, keyed on symbol — idempotent, used by
    the Nifty 500 seeding script (quarterly reconstitution), never the hot
    loop. Only the columns present in each row are written, so brain_score /
    trade stats owned by the paper engine are untouched."""
    if not rows:
        return 0
    try:
        database.supabase.table('stock_universe').upsert(
            rows, on_conflict='symbol').execute()
        return len(rows)
    except Exception as e:
        print(f"[upsert_stock_universe_bulk] error ({len(rows)} rows): {e}")
        return 0


def get_universe_by_sector(sector: str = None, exclude_symbols: list = None) -> list:
    """Active Nifty 500 rows for rotation-candidate lookup, best score first.
    sector=None returns the whole universe (cross-sector fallback)."""
    try:
        q = (database.supabase.table('stock_universe').select('*')
             .eq('is_active', True).eq('is_nifty500', True)
             .not_.is_('advisor_score', 'null'))
        if sector:
            q = q.eq('sector', sector)
        res = q.order('advisor_score', desc=True).execute()
        rows = res.data or []
        excl = set(exclude_symbols or [])
        return [r for r in rows if r.get('symbol') not in excl]
    except Exception as e:
        print(f"[get_universe_by_sector] error: {e}")
        return []


def get_top_scored_stocks(limit: int = 10) -> list:
    try:
        res = (
            database.supabase.table('stock_universe')
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
        res = database.supabase.table('stock_universe').select('*').eq('symbol', symbol).limit(1).execute()
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

        now = database._now_iso()
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
        database.supabase.table('stock_universe').update(updates).eq('symbol', symbol).execute()
        print(f"[database.update_stock_score] OK")
    except Exception as e:
        print(f"[database.update_stock_score] error {symbol}: {type(e).__name__}: {e}")


# --- M3 DATA LAYER (level pack, profiles, in-play) ---

def upsert_level_pack(row: dict) -> None:
    try:
        database.supabase.table('level_pack').upsert(
            row, on_conflict='symbol,date').execute()
        print(f"[level_pack] upsert {row.get('symbol')} {row.get('date')}")
    except Exception as e:
        print(f"[database.upsert_level_pack] error {row.get('symbol')}: {e}")


def upsert_stock_profile(row: dict) -> None:
    try:
        database.supabase.table('stock_profile').upsert(
            row, on_conflict='symbol,asof_date').execute()
        print(f"[stock_profile] upsert {row.get('symbol')} {row.get('asof_date')}")
    except Exception as e:
        print(f"[database.upsert_stock_profile] error {row.get('symbol')}: {e}")


def lock_inplay_list(date: str, ranked: list) -> int:
    """Write the day's in-play set (idempotent per date+symbol)."""
    written = 0
    for c in ranked:
        try:
            database.supabase.table('inplay_list').upsert({
                'date': date,
                'rank': c.get('rank'),
                'symbol': c.get('symbol'),
                'or_rvol': c.get('or_rvol'),
                'gap_pct': c.get('gap_pct'),
                'or_high': c.get('or_high'),
                'or_low': c.get('or_low'),
            }, on_conflict='date,symbol').execute()
            written += 1
        except Exception as e:
            print(f"[database.lock_inplay_list] error {c.get('symbol')}: {e}")
    print(f"[inplay] locked {written} symbols for {date}")
    return written


def level_pack_exists(date: str) -> bool:
    """Fails closed (True) — a DB error must not trigger a rebuild storm."""
    try:
        res = (database.supabase.table('level_pack').select('id')
               .eq('date', date).limit(1).execute())
        return bool(res.data)
    except Exception as e:
        print(f"[database.level_pack_exists] error: {e}")
        return True


def inplay_locked(date: str) -> bool:
    """Fails closed (True) — same rationale."""
    try:
        res = (database.supabase.table('inplay_list').select('id')
               .eq('date', date).limit(1).execute())
        return bool(res.data)
    except Exception as e:
        print(f"[database.inplay_locked] error: {e}")
        return True


def get_level_pack_map(date: str) -> dict:
    """All of today's level_pack rows keyed by symbol (for in-brain lookup)."""
    try:
        res = (database.supabase.table('level_pack').select('*')
               .eq('date', date).execute())
        return {r['symbol']: r for r in (res.data or [])}
    except Exception as e:
        print(f"[database.get_level_pack_map] error: {e}")
        return {}


def get_inplay_symbols(date: str) -> list:
    try:
        res = (database.supabase.table('inplay_list').select('symbol')
               .eq('date', date).order('rank').execute())
        return [r['symbol'] for r in (res.data or [])]
    except Exception as e:
        print(f"[database.get_inplay_symbols] error: {e}")
        return []


def get_directional_decisions_for_date(run_date: str) -> list:
    """BUY/SELL decisions for run_date not yet counterfactually labeled —
    the decision_outcomes (Track C) work queue. Excludes decisions already
    present in decision_outcomes so re-runs are cheap/idempotent.

    Paginated by hour: a full data-richness day is ~1k BUY/SELL rows each
    carrying a fat `indicators` jsonb, which overflows PostgREST's response
    payload limit in a single query ('JSON could not be generated' 400).
    Fetching an hour at a time keeps every response small — this is why the
    whole thing loops instead of doing one select (don't collapse it back)."""
    out = []
    try:
        for h in range(24):
            start = f'{run_date}T{h:02d}:00:00'
            end = f'{run_date}T{h + 1:02d}:00:00' if h < 23 \
                else f'{run_date}T23:59:59.999999'
            res = (database.supabase.table('brain_decisions')
                   .select('id, symbol, signal, price_at_decision, indicators, created_at')
                   .in_('signal', ['BUY', 'SELL'])
                   .gte('created_at', start).lt('created_at', end)
                   .execute())
            rows = res.data or []
            if not rows:
                continue
            ids = [r['id'] for r in rows]
            done = (database.supabase.table('decision_outcomes').select('decision_id')
                    .in_('decision_id', ids).execute())
            done_ids = {r['decision_id'] for r in (done.data or [])}
            out.extend(r for r in rows if r['id'] not in done_ids)
        return out
    except Exception as e:
        print(f"[get_directional_decisions_for_date] error: {e}")
        return out


def get_candles_for_symbol_from(symbol: str, after_ts: str, run_date: str) -> list:
    """5-min bars for symbol on run_date at/after after_ts, oldest first —
    the forward price-path a counterfactual decision_outcome walks."""
    try:
        res = (database.supabase.table('candles')
               .select('ts, open, high, low, close')
               .eq('symbol', symbol).eq('interval', '5minute')
               .eq('trade_date', run_date)
               .gte('ts', after_ts)
               .order('ts').execute())
        return res.data or []
    except Exception as e:
        print(f"[get_candles_for_symbol_from] error: {e}")
        return []


def insert_decision_outcome(row: dict) -> bool:
    """One counterfactual outcome row (Track C) — plain insert, decision_id
    is unique so a re-run of an already-labeled decision fails loudly
    rather than silently duplicating."""
    try:
        database.supabase.table('decision_outcomes').insert(row).execute()
        return True
    except Exception as e:
        print(f"[insert_decision_outcome] error for {row.get('decision_id')}: {e}")
        return False


def add_holdings_to_universe(holdings: list) -> None:
    for h in holdings:
        try:
            symbol = h.get('tradingsymbol')
            exchange = h.get('exchange')
            if not symbol:
                continue
            existing = database.supabase.table('stock_universe').select('symbol').eq('symbol', symbol).limit(1).execute()
            if existing.data and len(existing.data) > 0:
                continue
            database.supabase.table('stock_universe').insert({
                'symbol': symbol,
                'exchange': exchange,
                'is_nifty50': False,
                'is_active': True,
            }).execute()
        except Exception as e:
            print(f"[database.add_holdings_to_universe] error for {h.get('tradingsymbol')}: {e}")

