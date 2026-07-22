#!/usr/bin/env python3
"""Standalone advisor grading + factor attribution — run on demand instead
of waiting for the scheduler to happen to fire run_backtest_pass while a
token is live the exact day a verdict comes due.

WHY this exists: advisor_backtest.run_backtest_pass only ran inside the
scheduler's official-advisor path (scheduler.py). With the token gaps we've
had, a call could come due on a day nothing runs and silently never get
graded. This script grades every due row whenever you run it (any day a
token is pasted), so the track record actually accumulates — which is the
ONLY legitimate path to reweighting the advisor's 7 hand-picked factors
(VISION §7: change on evidence, never hand-tune).

Grading reads daily candles via market_data, so it needs a working enc_token
(same as a live advisor run — no orders, read-only). Factor attribution
(--attrib-only) reads already-graded rows from the DB and needs NO token.

Usage:
  python3 scripts/grade_advice.py              # grade due rows + print report
  python3 scripts/grade_advice.py --attrib-only  # skip grading, just the report (no token)
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import advisor_backtest
import database as db


def _print_track_record(summary: dict):
    n = summary.get('evaluated_calls', 0)
    print(f"\n=== Advisor track record ({n} graded calls) ===")
    if not n:
        print("No graded calls yet. The oldest official batch (2026-07-12) "
              "matures at its 10-trading-day MICRO horizon around 2026-07-24; "
              "MACRO calls need 30 trading days. Re-run then.")
        return
    print(f"hit rate: {summary['hit_rate_pct']}%   "
          f"avg return: {summary['avg_return_pct']}%   "
          f"avg alpha vs Nifty: {summary['avg_alpha_pct']}%   "
          f"advice value: ₹{summary['advice_value_inr']:,.0f}")
    for v, s in sorted(summary.get('by_verdict', {}).items()):
        hr = round(s['hits'] / s['calls'] * 100, 1) if s['calls'] else 0
        print(f"  {v:<16} {s['calls']:>3} calls  {hr:>5}% right")


def _print_attribution(attrib: dict):
    n = attrib.get('graded_calls', 0)
    print(f"\n=== Factor attribution ({n} graded calls) ===")
    if not n:
        print("Needs graded calls first (see above). Once ~30-50 calls are "
              "judged, this ranks which of the 7 scoring factors actually "
              "separated right calls from wrong — the evidence to reweight on.")
        return
    print("Ranked by hit-rate separation (how much the factor splits right "
          "from wrong calls — a high spread earns its weight, near-zero "
          "doesn't):\n")
    for name in attrib['ranked_by_separation']:
        f = attrib['factors'][name]
        print(f"{name}  (separation {f['separation_pct']}pp)")
        for label, b in sorted(f['buckets'].items(),
                               key=lambda kv: (kv[1]['hit_rate_pct'] or 0),
                               reverse=True):
            flag = ' [low-n]' if b['low_n'] else ''
            alpha = f"{b['avg_alpha_pct']:+.2f}%" if b['avg_alpha_pct'] is not None else 'n/a'
            print(f"    {label:<22} n={b['n']:<3} hit={b['hit_rate_pct']}%  "
                  f"alpha={alpha}{flag}")
    unranked = [n for n in attrib['factors']
                if attrib['factors'][n]['separation_pct'] is None]
    if unranked:
        print(f"\n  (not yet rankable — <2 buckets with enough sample: "
              f"{', '.join(unranked)})")


def main(attrib_only: bool):
    if not attrib_only:
        token = db.get_enc_token()
        if not token:
            print("No enc_token stored — cannot grade (needs read-only candle "
                  "access). Paste a token, or use --attrib-only for the "
                  "report over already-graded rows.")
        else:
            from kite_client import KiteClient
            from market_data import MarketData
            md = MarketData(KiteClient(token))
            graded = advisor_backtest.run_backtest_pass(md)
            print(f"[grade_advice] graded {graded} newly-due advice rows")

    _print_track_record(advisor_backtest.get_track_record_summary())
    _print_attribution(advisor_backtest.factor_attribution(
        db.get_evaluated_advice_with_features()))


if __name__ == '__main__':
    main(attrib_only='--attrib-only' in sys.argv[1:])
