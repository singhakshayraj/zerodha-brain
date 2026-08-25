"""[P-25] Real-money accountability — what the USER actually did.

The advisor has always been able to say what it *recommended*. It has never
been able to say what was *done about it*: `portfolio_advice.user_decision` is
written only by the Telegram bot (blocked on [P-04] creds) — 8 rows out of
2,378 ever set. The paper books simulate advice; they do not record reality.

**No new capture is needed.** `portfolio_advice` already writes symbol +
quantity + avg_price for every holding on every run (~8 min cadence), which is
an implicit holdings time series. Diffing consecutive runs recovers real
executions at that resolution, and links each one back to the advice that was
standing at the time.

Validated against ground truth before being written: across ~183 runs and 25
symbols of history the detector fires exactly once — `NBCC SELL 115` at
2026-08-06 04:36 UTC — which is precisely the sale the user reported, with zero
false positives.

── Known asymmetry, by construction ────────────────────────────────────────
Zerodha's `holdings` feed reports **delivered** stock only. A BUY therefore
does not appear until settlement (T+1), while a SELL is visible on the next
run because the holding shrinks or disappears. So:
  * SELL  → detected within ~8 minutes, reliably.
  * BUY   → detected next session at the earliest, and never at all if the
            position was opened and closed intraday.
This is why the 08-06 rotation *sell* was recovered but its paired *buy* was
not: no rotation target ever entered the holdings feed. Do not "fix" the
detector for that — it is a property of the source, and the honest response is
to label BUY detections as late rather than to pretend they are prompt.
"""

import pytz

import database as db

IST = pytz.timezone('Asia/Kolkata')

# A run holding fewer than this fraction of the previous run's symbols is
# treated as a TORN SNAPSHOT, not as a mass liquidation. Without this guard a
# single failed holdings fetch would be recorded as the user selling their
# entire portfolio — the most damaging false positive this module can produce.
MIN_SNAPSHOT_RATIO = 0.5

SELL_VERDICTS = ('SELL', 'SELL_ON_BOUNCE', 'TRIM')


def _buy_fill_price(prev_qty: int, prev_avg, qty: int, avg):
    """Exact fill price of a BUY, recovered from the average-cost shift:

        avg_new * qty_new = avg_old * qty_old + fill * (qty_new - qty_old)

    Returns None when it cannot be derived (missing averages, or a brand-new
    holding where avg_new IS the fill price and the caller uses that instead).
    """
    try:
        d = qty - prev_qty
        if d <= 0 or prev_avg is None or avg is None:
            return None
        fill = (float(avg) * qty - float(prev_avg) * prev_qty) / d
        return round(fill, 4) if fill > 0 else None
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def detect_executions(snapshots: list) -> list:
    """Diff consecutive holdings snapshots into inferred executions.

    `snapshots`: list of {'run_id', 'run_at', 'holdings': {sym: {...}}},
    ascending by run_at. Pure — no DB, no clock — so the guards below are
    testable without a live account.
    """
    out = []
    prev = None
    for snap in snapshots:
        holdings = snap.get('holdings') or {}
        if prev is None:
            if holdings:
                prev = snap
            continue

        prev_h = prev['holdings']
        # Torn-snapshot guard. Compared against the PREVIOUS good run, so a
        # genuine gradual sell-down still registers while a collapsed fetch
        # is skipped and the next run diffs against the last good one.
        if prev_h and len(holdings) < len(prev_h) * MIN_SNAPSHOT_RATIO:
            continue

        for sym in sorted(set(prev_h) | set(holdings)):
            before = prev_h.get(sym) or {}
            after = holdings.get(sym) or {}
            q0 = int(before.get('quantity') or 0)
            q1 = int(after.get('quantity') or 0)
            if q0 == q1:
                continue

            if q1 < q0:
                # SELL. The broker leaves avg_price untouched on a sale, so
                # there is no fill price to recover — last_price at the run is
                # the closest available estimate and is flagged as such.
                px = after.get('last_price') or before.get('last_price')
                out.append({
                    'symbol': sym, 'side': 'SELL', 'quantity': q0 - q1,
                    'price': float(px) if px else None,
                    'price_is_estimated': True,
                    'detected_at': snap['run_at'],
                    'prev_run_id': prev['run_id'], 'run_id': snap['run_id'],
                    'notes': 'position closed' if q1 == 0 else 'partial sell',
                })
            else:
                # BUY. A brand-new holding carries the true average cost in
                # avg_price; an add is recoverable from the average shift.
                if q0 == 0:
                    px = after.get('avg_price')
                    est = False
                else:
                    px = _buy_fill_price(q0, before.get('avg_price'),
                                         q1, after.get('avg_price'))
                    est = px is None
                    if px is None:
                        px = after.get('avg_price')
                out.append({
                    'symbol': sym, 'side': 'BUY', 'quantity': q1 - q0,
                    'price': float(px) if px else None,
                    'price_is_estimated': est,
                    'detected_at': snap['run_at'],
                    'prev_run_id': prev['run_id'], 'run_id': snap['run_id'],
                    'notes': 'new position (delivered, so T+1 at the earliest)'
                             if q0 == 0 else 'added to position',
                })
        prev = snap
    return out


def _followed_advice(side: str, verdict: str, was_rotation_target: bool):
    """Did the execution go the way the advice pointed? None = no opinion.

    Deliberately conservative: a SELL against a HOLD verdict is recorded as
    NOT following, but a BUY of a name the advisor never mentioned gets None
    rather than False — the advisor said nothing, so it is neither obeyed nor
    defied, and scoring it either way would corrupt the track record.
    """
    v = (verdict or '').upper()
    if side == 'SELL':
        if v in SELL_VERDICTS:
            return True
        if v == 'HOLD':
            return False
        return None
    if was_rotation_target:
        return True
    return None


def sync_user_executions(lookback_days: int = None) -> int:
    """Detect and persist executions. Idempotent — the table's natural key
    (symbol, side, quantity, detected_at) makes re-runs and the backfill safe
    to repeat. Returns the number of NEW rows written."""
    snapshots = db.get_advice_snapshots(lookback_days=lookback_days)
    if not snapshots:
        print('[user_exec] no advice snapshots — nothing to diff')
        return 0

    execs = detect_executions(snapshots)
    if not execs:
        print(f'[user_exec] {len(snapshots)} snapshots, no holdings changes')
        return 0

    rows = []
    for e in execs:
        advice = db.get_advice_at(e['symbol'], e['detected_at'])
        target_of = db.was_rotation_target(e['symbol'], e['detected_at'])
        verdict = (advice or {}).get('verdict')
        followed = _followed_advice(e['side'], verdict, target_of)
        rows.append({
            **e,
            'advice_id': (advice or {}).get('id'),
            'verdict_at_time': verdict,
            'followed_advice': followed,
            'inferred': True,
        })

    written = db.insert_user_executions(rows)

    # Stamp the advice row the user effectively acted on. Only when they
    # followed it — `record_advice_decision` already refuses rows the backtest
    # has scored, so a late inference can never move a call between the
    # accepted/declined buckets after the fact.
    stamped = 0
    for r in rows:
        if r['followed_advice'] and r.get('advice_id'):
            run_date = str(r['detected_at'])[:10]
            if db.record_advice_decision(run_date, r['symbol'], 'accept'):
                stamped += 1

    print(f'[user_exec] {len(snapshots)} snapshots → {len(execs)} executions, '
          f'{written} new, {stamped} advice rows stamped')
    return written


def run_user_executions(lookback_days: int = 5) -> bool:
    """Scheduler entry point, called on every advisor pass so a SELL surfaces
    within one refresh (~8 min). Bounded lookback keeps it cheap on the hot
    path — the full-history sweep is `scripts/backfill_user_executions.py`.
    Never raises: accountability bookkeeping must not be able to take down a
    trading session."""
    try:
        sync_user_executions(lookback_days=lookback_days)
        return True
    except Exception as e:
        print(f'[user_exec] sync failed (non-fatal): {e}')
        return False
