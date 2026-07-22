#!/usr/bin/env python3
"""Pacing-cost replay (counterfactual-audit §5) — prices what the
data-richness pacing gates (CONCURRENT_CAP, CYCLE_LIMIT, SYMBOL_DAY_CAP,
HOURLY_PACE) cost or saved, using each deferred signal's own
decision_outcomes row (Track C: stop/target walked forward through the
candle archive from decision time to close).

Requires: decision_outcomes populated for the date (see
scripts/label_decisions.py) and candles present for that date — deferred
signals on a day with the pre-07-15 candle-archive bug label NO_DATA and
are silently excluded (reported separately, not priced).

Rupee figures are approximate: this system sizes positions via Kelly
(risk_manager.calculate_position_size), not a flat 1% of capital, so
there's no single ₹-per-R conversion. We use the day's own realized
avg risk-per-trade (from `trades`) as the closest available estimate —
report R-multiples as the primary, exact number.

Usage: python3 scripts/pacing_cost.py 2026-07-22 [2026-07-23 ...]
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import database as db


def _avg_risk_rupees(run_date: str):
    res = (db.supabase.table('trades')
           .select('quantity, entry_price, stop_loss_price')
           .gte('created_at', f'{run_date}T00:00:00')
           .lt('created_at', f'{run_date}T23:59:59.999999')
           .execute())
    rows = res.data or []
    risks = [
        abs(r['quantity'] * (r['entry_price'] - r['stop_loss_price']))
        for r in rows
        if r.get('quantity') and r.get('entry_price') and r.get('stop_loss_price')
    ]
    return (sum(risks) / len(risks)) if risks else None


def _deferred_with_outcomes(run_date: str):
    """decision_id -> (reasons set, r_multiple, outcome) for every
    ENTRY_DEFERRED decision on run_date that has a Track C label."""
    deferred = {}
    page_size = 1000
    offset = 0
    while True:
        res = (db.supabase.table('brain_decisions')
               .select('id, skip_reasons')
               .gte('created_at', f'{run_date}T00:00:00')
               .lt('created_at', f'{run_date}T23:59:59.999999')
               .not_.is_('skip_reasons', 'null')
               .range(offset, offset + page_size - 1)
               .execute())
        rows = res.data or []
        for row in rows:
            reasons = [r.split(':', 1)[1] for r in (row.get('skip_reasons') or [])
                       if r.startswith('ENTRY_DEFERRED:')]
            if reasons:
                deferred[row['id']] = reasons
        if len(rows) < page_size:
            break
        offset += page_size
    if not deferred:
        return {}

    ids = list(deferred.keys())
    outcomes = {}
    for i in range(0, len(ids), 200):  # PostgREST payload limit, same as label_decisions
        batch = ids[i:i + 200]
        res = (db.supabase.table('decision_outcomes')
               .select('decision_id, r_multiple, outcome')
               .in_('decision_id', batch).execute())
        for r in (res.data or []):
            outcomes[r['decision_id']] = r
    return {
        did: (deferred[did], outcomes.get(did))
        for did in deferred
    }


def report(run_date: str):
    print(f"\n=== Pacing cost — {run_date} ===")
    joined = _deferred_with_outcomes(run_date)
    if not joined:
        print("no ENTRY_DEFERRED decisions this date")
        return

    avg_risk = _avg_risk_rupees(run_date)
    per_reason = {}
    no_data = 0
    for did, (reasons, outcome) in joined.items():
        if outcome is None or outcome.get('r_multiple') is None:
            no_data += len(reasons)
            continue
        r = outcome['r_multiple']
        win = 1 if outcome['outcome'] == 'WIN' else 0
        loss = 1 if outcome['outcome'] == 'LOSS' else 0
        for reason in reasons:
            agg = per_reason.setdefault(reason, {'n': 0, 'wins': 0, 'losses': 0, 'total_r': 0.0})
            agg['n'] += 1
            agg['wins'] += win
            agg['losses'] += loss
            agg['total_r'] += r

    if no_data:
        print(f"({no_data} deferred signal-reason pairs had no priceable "
              f"candle path — NO_DATA, excluded)")

    print(f"{'reason':<20} {'n':>4} {'W':>3} {'L':>3} {'total_R':>9} {'avg_R':>7} "
          f"{'~rupees':>10}  verdict")
    for reason, a in sorted(per_reason.items()):
        avg_r = a['total_r'] / a['n']
        rupees = a['total_r'] * avg_risk if avg_risk else None
        rupee_s = f"{rupees:+.2f}" if rupees is not None else "n/a"
        verdict = ('HELPED (blocked a net loser)' if a['total_r'] < 0
                   else 'COST (blocked a net winner)' if a['total_r'] > 0
                   else 'NEUTRAL')
        print(f"{reason:<20} {a['n']:>4} {a['wins']:>3} {a['losses']:>3} "
              f"{a['total_r']:>9.3f} {avg_r:>7.3f} {rupee_s:>10}  {verdict}")

    if avg_risk:
        print(f"\n(₹ column uses this date's realized avg risk/trade "
              f"₹{avg_risk:.2f} as the R->rupee conversion — approximate, "
              f"sizing is Kelly-scaled per trade, not flat)")
    else:
        print("\n(no real trades this date to derive a ₹/R conversion — R-multiples only)")


if __name__ == '__main__':
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    for d in sys.argv[1:]:
        report(d)
