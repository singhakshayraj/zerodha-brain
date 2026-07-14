"""Track C (2026-07-15): counterfactual outcome labeling for EVERY
directional decision (BUY/SELL), not just the ~1% that became a real
trade. See docs/ML_TRACK_C_NOTES.md (zerodha-trading repo) for the full
design discussion.

For each BUY/SELL decision, its own logged stop_loss/target/
price_at_decision are treated as if that decision had been taken as a
fresh entry, walked forward through the 5-min candle archive using the
same stop-before-target priority order the live exit logic uses
(brain.py::_evaluate_exit). This is a COUNTERFACTUAL, not a real fill:
it ignores whether concurrent-position/capital limits would have allowed
the entry, and ignores slippage/costs (unlike trades.r_multiple, which
already bakes those in via the paper broker). Still valuable — it turns
a ~14-trades/day dataset into a ~hundreds-of-decisions/day dataset.

Offline/on-demand only (not wired into the live scheduler) — run via
scripts/label_decisions.py.

KNOWN GAP: only meaningful from 2026-07-15 onward. The candle-archive
batch-dedup bug (fixed 2026-07-14 post-close) means `candles` has zero
rows for 07-14 and earlier — those decisions label as NO_DATA.
"""
import database as db

IST_MARKET_CLOSE = (15, 20)


def _walk_forward(direction: str, entry: float, stop: float, target: float,
                   candles: list):
    """Returns (exit_price, exit_reason, bars_used). Same-bar stop+target
    ambiguity (a single 5-min bar's high/low straddles both) resolves
    conservatively as stop-first, matching the live priority order —
    we can't know the true intrabar sequence from OHLC alone."""
    if not candles:
        return None, 'NO_DATA', 0
    for i, c in enumerate(candles, start=1):
        lo, hi = float(c['low']), float(c['high'])
        if direction == 'LONG':
            if lo <= stop:
                return stop, 'STOP_HIT', i
            if hi >= target:
                return target, 'TARGET_HIT', i
        else:  # SHORT
            if hi >= stop:
                return stop, 'STOP_HIT', i
            if lo <= target:
                return target, 'TARGET_HIT', i
    return float(candles[-1]['close']), 'SESSION_END', len(candles)


def _r_multiple(direction: str, entry: float, stop: float, exit_price: float):
    """Price-based R (no qty/capital needed — quantity cancels out)."""
    risk = abs(entry - stop)
    if risk <= 0:
        return None
    if direction == 'LONG':
        return round((exit_price - entry) / risk, 3)
    return round((entry - exit_price) / risk, 3)


def label_one(decision: dict) -> dict:
    """Computes (but does not store) the outcome row for one decision dict
    as returned by db.get_directional_decisions_for_date. Split out from
    label_decisions_for_date for direct unit testing without the DB."""
    ind = decision.get('indicators') or {}
    symbol = decision['symbol']
    entry = decision.get('price_at_decision')
    stop = ind.get('stop_loss')
    target = ind.get('target')
    run_date = str(decision['created_at'])[:10]

    if entry is None or stop is None or target is None:
        return {
            'decision_id': decision['id'], 'symbol': symbol,
            'run_date': run_date,
            'direction': 'LONG' if decision['signal'] == 'BUY' else 'SHORT',
            'entry_price': entry or 0, 'stop_price': stop or 0,
            'target_price': target or 0, 'exit_price': None,
            'exit_reason': 'NO_DATA', 'r_multiple': None,
            'outcome': None, 'bars_used': 0,
        }

    direction = 'LONG' if decision['signal'] == 'BUY' else 'SHORT'
    candles = db.get_candles_for_symbol_from(
        symbol, decision['created_at'], run_date)
    exit_price, exit_reason, bars_used = _walk_forward(
        direction, float(entry), float(stop), float(target), candles)

    r = None
    outcome = None
    if exit_price is not None:
        r = _r_multiple(direction, float(entry), float(stop), exit_price)
        if r is not None:
            outcome = 'WIN' if r > 0 else ('LOSS' if r < 0 else 'UNRESOLVED')
        if exit_reason == 'SESSION_END' and outcome is None:
            outcome = 'UNRESOLVED'

    return {
        'decision_id': decision['id'], 'symbol': symbol, 'run_date': run_date,
        'direction': direction, 'entry_price': float(entry),
        'stop_price': float(stop), 'target_price': float(target),
        'exit_price': exit_price, 'exit_reason': exit_reason,
        'r_multiple': r, 'outcome': outcome, 'bars_used': bars_used,
    }


def label_decisions_for_date(run_date: str) -> int:
    """Labels every not-yet-labeled BUY/SELL decision for run_date.
    Per-decision failures skip that decision, never abort the run.
    Returns count stored."""
    decisions = db.get_directional_decisions_for_date(run_date)
    stored = 0
    for d in decisions:
        try:
            row = label_one(d)
            if db.insert_decision_outcome(row):
                stored += 1
        except Exception as e:
            print(f"[decision_outcomes] {d.get('symbol')} "
                  f"{d.get('id')} failed (skipped): {e}")
    print(f"[decision_outcomes] labeled {stored}/{len(decisions)} "
          f"decisions for {run_date}")
    return stored
