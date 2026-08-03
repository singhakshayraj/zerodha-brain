"""Recording / logging data access — decisions, quote snapshots, candles,
news, tradebook, and advice-grading queue (SE4 module split part 2, P-06).
Extracted from database.py. References the shared client on the `database`
module at call time (`database.supabase`) so tests that patch `database.supabase`
still intercept these; database.py re-exports every name so `db.<name>` callers
are unchanged. `_fetch_all` (pagination helper) lives here with its only users.
"""
from datetime import datetime, timezone  # noqa: F401  (used by moved fns)

import database

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
) -> str:
    """Log decision; extra fields merged into indicators JSONB. Returns the
    inserted decision id (or None) so an entry can link its trade back to the
    exact feature vector that produced it."""
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

        res = database.supabase.table('brain_decisions').insert(payload).execute()
        print(f"[log_decision] OK {symbol} {signal} conf={confidence}")
        if res.data and len(res.data) > 0:
            return res.data[0].get('id')
        return None

    except Exception as e:
        print(f"[log_decision] error: {e}")
        print(f"[log_decision] symbol={symbol}")
        return None


def link_decision_trade(decision_id: str, trade_id: str) -> None:
    """Stamp the trade a decision produced back onto the decision row, so the
    35-feature decision snapshot joins to its realized outcome (pnl,
    r_multiple). brain_decisions.trade_id was never populated — the missing
    link that blocked supervised (features -> outcome) learning."""
    if not decision_id or not trade_id:
        return
    try:
        database.supabase.table('brain_decisions').update(
            {'trade_id': trade_id}).eq('id', decision_id).execute()
    except Exception as e:
        print(f"[link_decision_trade] error decision={decision_id}: {e}")


def log_quote_snapshot(session_id: str, cycle: int, prices: dict) -> None:
    """One row per cycle: jsonb map of symbol -> LTP for the scanned
    universe, so training can reconstruct what the brain saw."""
    try:
        database.supabase.table('quote_snapshots').insert({
            'session_id': session_id,
            'cycle': int(cycle),
            'prices': prices,
        }).execute()
        print(f"[log_quote_snapshot] OK cycle={cycle} symbols={len(prices)}")
    except Exception as e:
        print(f"[log_quote_snapshot] error: {e}")


def candle_rows(session_id: str, symbol: str, exchange: str,
                candles: list, interval: str = '5minute',
                tail: int = 3) -> list:
    """Format the trailing `tail` OHLCV bars into candle-archive rows (pure —
    no DB). Only the last few bars: the forming bar finalizes over successive
    cycles, older closed bars don't change, and the (symbol,interval,ts)
    upsert dedups a bar however many cycles re-saw it."""
    if not candles:
        return []
    rows = []
    for c in candles[-tail:]:
        ts = c.get('timestamp')
        if not ts:
            continue
        try:
            rows.append({
                'symbol': symbol,
                'exchange': exchange or 'NSE',
                'interval': interval,
                'ts': ts,
                'trade_date': str(ts)[:10],   # ISO 'YYYY-MM-DD...' (IST trading day)
                'open': c.get('open'),
                'high': c.get('high'),
                'low': c.get('low'),
                'close': c.get('close'),
                'volume': int(c.get('volume') or 0),
                'session_id': session_id,
                'updated_at': datetime.now(timezone.utc).isoformat(),
            })
        except Exception as e:
            print(f"[candle_rows] skip bad bar {symbol} {ts}: {e}")
    return rows


def upsert_candles(rows: list) -> int:
    """One bulk upsert of the whole cycle's candle rows — a single round-trip
    instead of one-per-symbol (that per-symbol version added ~7s/cycle in
    prod, slowing stop detection). quote_snapshots keeps only last-price;
    this keeps the actual bars, the substrate for a replay/backtest harness
    (M5) and price-action models that can't be reliably re-fetched later.

    Rows are deduped on (symbol, interval, ts) first — a symbol analyzed
    twice in one cycle (holdings AND nifty50 universe entries) produces the
    same bars twice, and Postgres rejects a single upsert that touches one
    row twice ('cannot affect row a second time'), losing the WHOLE batch.
    Seen live 2026-07-14: 141-row cycles archived nothing."""
    if not rows:
        return 0
    try:
        deduped = list({(r['symbol'], r['interval'], str(r['ts'])): r
                        for r in rows}.values())
        database.supabase.table('candles').upsert(
            deduped, on_conflict='symbol,interval,ts').execute()
        return len(deduped)
    except Exception as e:
        print(f"[upsert_candles] error ({len(rows)} rows): {e}")
        return 0


def archive_candles(session_id: str, symbol: str, exchange: str,
                    candles: list, interval: str = '5minute',
                    tail: int = 3) -> int:
    """Single-symbol convenience: format + upsert one symbol's bars."""
    return upsert_candles(
        candle_rows(session_id, symbol, exchange, candles, interval, tail))


# --- NEWS EVENTS (NEWS_CORRELATION_PLAN) ---

def upsert_news_events(rows: list) -> int:
    """Bulk upsert news rows, deduped on (source, url). Decoupled from the
    trading loop — the collector calls this out of band."""
    if not rows:
        return 0
    try:
        database.supabase.table('news_events').upsert(
            rows, on_conflict='source,url').execute()
        return len(rows)
    except Exception as e:
        print(f"[upsert_news_events] error ({len(rows)} rows): {e}")
        return 0


def traded_symbols() -> list:
    """Distinct symbols we've actually traded — the backfill target set."""
    try:
        res = database.supabase.table('trades').select('symbol').execute()
        return sorted({r['symbol'] for r in (res.data or []) if r.get('symbol')})
    except Exception as e:
        print(f"[traded_symbols] error: {e}")
        return []


def upsert_tradebook(rows: list) -> int:
    """Append real-account trades, deduped on (exchange, trade_id, order_id) —
    safe to re-run for the same day."""
    if not rows:
        return 0
    try:
        database.supabase.table('tradebook').upsert(
            rows, on_conflict='exchange,trade_id,order_id',
            ignore_duplicates=True).execute()
        return len(rows)
    except Exception as e:
        print(f"[upsert_tradebook] error ({len(rows)} rows): {e}")
        return 0


def _fetch_all(query, page_size: int = 1000) -> list:
    """Drain a PostgREST select past the 1000-row server default — an
    un-paginated .execute() silently truncates and the caller computes on a
    partial dataset (KNOWN_ISSUES P2). Query must already be ordered."""
    out = []
    start = 0
    while True:
        res = query.range(start, start + page_size - 1).execute()
        batch = res.data or []
        out.extend(batch)
        if len(batch) < page_size:
            return out
        start += page_size


def get_tradebook() -> list:
    """Full real-account trade history (import + daily appends)."""
    try:
        return _fetch_all(
            database.supabase.table('tradebook')
            .select('symbol, trade_type, quantity, price, trade_date')
            .order('executed_at'))
    except Exception as e:
        print(f"[get_tradebook] error: {e}")
        return []


def append_decision_skip(decision_id: str, reason: str) -> bool:
    """Append a post-hoc skip reason (e.g. ENTRY_DEFERRED:HOURLY_PACE) onto
    an already-logged decision row, so 'what did pacing cost us' is
    answerable from brain_decisions alone (KNOWN_ISSUES P4). Read-modify-
    write is fine here: one writer, low volume (deferred entries only)."""
    if not decision_id:
        return False
    try:
        res = (database.supabase.table('brain_decisions').select('skip_reasons')
               .eq('id', decision_id).limit(1).execute())
        if not res.data:
            return False
        existing = res.data[0].get('skip_reasons') or []
        if reason in existing:
            return True
        (database.supabase.table('brain_decisions')
         .update({'skip_reasons': existing + [reason]})
         .eq('id', decision_id).execute())
        return True
    except Exception as e:
        print(f"[append_decision_skip] {decision_id} error: {e}")
        return False


def get_unevaluated_advice(max_run_date: str) -> list:
    """Advice rows old enough to judge (run_date <= max_run_date) that have
    no outcome yet — the backtest work queue. Scoped to is_official: the
    intraday refresh (2026-07-14) writes several extra snapshot rows per
    symbol/day, and only the one official daily row is backtest-eligible."""
    try:
        res = (database.supabase.table('portfolio_advice').select('*')
               .is_('evaluated_at', 'null')
               .eq('is_official', True)
               .lte('run_date', max_run_date)
               .order('run_date').execute())
        return res.data or []
    except Exception as e:
        print(f"[get_unevaluated_advice] error: {e}")
        return []


def update_advice_outcome(run_date: str, symbol: str, outcome: dict) -> bool:
    """Write one row's realized outcome (evaluated_at + outcome_* columns).
    Scoped to is_official so this can't ever touch an intraday snapshot row
    sharing the same (run_date, symbol)."""
    try:
        (database.supabase.table('portfolio_advice').update(outcome)
         .eq('run_date', run_date).eq('symbol', symbol)
         .eq('is_official', True).execute())
        return True
    except Exception as e:
        print(f"[update_advice_outcome] {run_date}/{symbol} error: {e}")
        return False


def record_advice_decision(run_date: str, symbol: str, decision: str) -> bool:
    """Store the user's Accept/Decline tap on one advice row. Idempotent —
    a re-tap overwrites with the latest choice, but ONLY while the row is
    still unjudged: once the backtest has evaluated it (evaluated_at set),
    the decision is frozen — a stale tap on an old digest can't move a call
    between the accepted/declined track-record buckets after the outcome
    was already scored against it. DECISION ONLY: one text column changes;
    nothing here touches an order path."""
    if decision not in ('accept', 'decline'):
        return False
    try:
        from datetime import datetime

        import pytz
        now = datetime.now(pytz.timezone('Asia/Kolkata')).isoformat()
        res = (database.supabase.table('portfolio_advice')
               .update({'user_decision': decision, 'decided_at': now})
               .eq('run_date', run_date).eq('symbol', symbol)
               .eq('is_official', True)
               .is_('evaluated_at', 'null').execute())
        if not res.data:
            print(f"[record_advice_decision] {run_date}/{symbol} rejected "
                  f"(already evaluated or no such row)")
        return bool(res.data)
    except Exception as e:
        print(f"[record_advice_decision] {run_date}/{symbol} error: {e}")
        return False


def get_evaluated_advice() -> list:
    """All judged advice rows — the advisor's track record. is_official is
    redundant here in practice (only official rows are ever evaluated — see
    get_unevaluated_advice) but kept explicit for clarity."""
    try:
        return _fetch_all(
            database.supabase.table('portfolio_advice')
            .select('run_date, symbol, verdict, trend_score, quantity, '
                    'last_price, outcome_return_pct, outcome_vs_nifty_pct, '
                    'outcome_correct, evaluated_at, user_decision')
            .eq('is_official', True)
            .not_.is_('evaluated_at', 'null')
            .order('run_date'))
    except Exception as e:
        print(f"[get_evaluated_advice] error: {e}")
        return []


def get_evaluated_advice_with_features() -> list:
    """Judged advice rows WITH the per-decision `indicators` blob + trigger
    type — the substrate for factor attribution (which scoring factor
    actually separated right calls from wrong ones). Separate from
    get_evaluated_advice so the lightweight track-record summary doesn't
    haul the jsonb on every call."""
    try:
        return _fetch_all(
            database.supabase.table('portfolio_advice')
            .select('run_date, symbol, verdict, trend_score, trigger_type, '
                    'confidence, last_price, indicators, outcome_correct, '
                    'outcome_return_pct, outcome_vs_nifty_pct')
            .eq('is_official', True)
            .not_.is_('evaluated_at', 'null')
            .order('run_date'))
    except Exception as e:
        print(f"[get_evaluated_advice_with_features] error: {e}")
        return []
