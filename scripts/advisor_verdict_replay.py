#!/usr/bin/env python3
"""Part B — out-of-sample replay of the EXACT current advisor verdict.

The live read shows a 0.631 alpha hit rate on 187 graded calls, but those calls
span ~1 independent forward window, so the nominal CI is wrong and the result
is unresolved. This runs advisor_scoring.advise() UNCHANGED across the 5.2-year
daily cache, grades each verdict with the same market-neutral alpha rule the
grader uses, on NON-OVERLAPPING windows, and t-tests hit rate across dates
(Fama-MacBeth) — the honest standard error.

NOT advisor_lab.py: that tested scoring-weight IC. This tests VERDICT hit rate
under the alpha label — a different object, never run.

Survivorship caveat: the cache is CURRENT Nifty-500 constituents, so results
are biased TOWARD finding skill. A null here is therefore stronger than it
looks; a positive is weaker.

Usage:  python3 scripts/advisor_verdict_replay.py [--symbols N]
"""
import argparse
import os
import pickle
import statistics
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from advisor_scoring import advise, MIN_DAILY_BARS
from advisor_backtest import _EXIT_VERDICTS          # exact verdict->grade rule
from scripts.advisor_lab import _t_and_p             # Fama-MacBeth t (reused)

CACHE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     'data', 'daily_history.pkl')
BENCH = 'NSE:NIFTY 50'


def _closes(bars):
    return [float(b['close']) for b in bars if b.get('close') is not None]


def _grade(verdict, fwd_alpha):
    """Same rule as advisor_backtest.evaluate_verdict's alpha label."""
    if verdict == 'HOLD':
        return fwd_alpha > 0
    if verdict in _EXIT_VERDICTS:
        return fwd_alpha < 0
    return None                                       # INSUFFICIENT / unknown


def replay(horizon, limit=None):
    cache = pickle.load(open(CACHE, 'rb'))
    bench = cache.get(BENCH) or []
    bench_by_date = {b['timestamp'][:10]: float(b['close']) for b in bench
                     if b.get('close') is not None}
    grid = sorted(bench_by_date)
    # NON-OVERLAPPING rebalance dates: stride = horizon, leaving MIN_DAILY_BARS
    # of history behind and `horizon` forward.
    reb_idx = list(range(MIN_DAILY_BARS, len(grid) - horizon, horizon))
    reb_dates = [grid[i] for i in reb_idx]

    symbols = sorted(s for s in cache if s != BENCH)
    if limit:
        symbols = symbols[:limit]

    per_date = {}                                     # date -> [(correct_bool, alpha)]
    for si, sym in enumerate(symbols):
        bars = [b for b in (cache.get(sym) or [])
                if b.get('close') is not None and b['timestamp'][:10] in bench_by_date]
        if len(bars) < MIN_DAILY_BARS + horizon + 1:
            continue
        closes = _closes(bars)
        dates = [b['timestamp'][:10] for b in bars]
        didx = {d: i for i, d in enumerate(dates)}
        for d in reb_dates:
            i = didx.get(d)
            if i is None or i < MIN_DAILY_BARS or i + horizon >= len(bars):
                continue
            window = bars[:i + 1]
            w_closes = closes[:i + 1]
            ndates = dates[:i + 1]
            nifty_closes = [bench_by_date[x] for x in ndates]
            try:
                v = advise({'symbol': sym, 'quantity': 0, 'average_price': None,
                            'last_price': w_closes[-1]},
                           window, nifty_closes=nifty_closes)
            except Exception:
                continue
            verdict = v.get('verdict')
            if verdict == 'INSUFFICIENT' or verdict is None:
                continue
            stock_ret = (closes[i + horizon] / closes[i] - 1) * 100
            b0, b1 = bench_by_date[d], bench_by_date[dates[i + horizon]]
            nifty_ret = (b1 / b0 - 1) * 100
            fwd_alpha = stock_ret - nifty_ret
            correct = _grade(verdict, fwd_alpha)
            if correct is None:
                continue
            per_date.setdefault(d, []).append((correct, fwd_alpha))
    return per_date, len(reb_dates)


def summarise(per_date, horizon, n_reb):
    dates = sorted(d for d in per_date if len(per_date[d]) >= 5)
    hit_series, alpha_series = [], []
    total = 0
    for d in dates:
        rs = per_date[d]
        total += len(rs)
        hit_series.append(sum(1 for c, _ in rs if c) / len(rs))
        alpha_series.append(statistics.fmean(a for _, a in rs))
    pooled_hit = (sum(sum(1 for c, _ in per_date[d] if c) for d in dates) / total
                  if total else float('nan'))
    t_hit, p_hit = _t_and_p([h - 0.50 for h in hit_series])
    t_alpha, p_alpha = _t_and_p(alpha_series)
    print(f"\n===== {'MACRO' if horizon == 30 else 'MICRO'} horizon = {horizon}d "
          f"(non-overlapping) =====")
    print(f"  rebalance dates available: {n_reb} | with >=5 verdicts: {len(dates)} "
          f"(THE REAL n)")
    print(f"  total graded verdicts: {total}")
    print(f"  pooled hit rate: {pooled_hit:.4f}  (naive, ignore)")
    print(f"  across-date mean hit: {statistics.fmean(hit_series):.4f}  "
          f"sd {statistics.stdev(hit_series):.4f}")
    print(f"  FAMA-MACBETH t(hit - 0.50): {t_hit:+.2f}  (p={p_hit:.3f})   "
          f"-> {'SURVIVES' if abs(t_hit) >= 2 else 'does NOT survive'} |t|>=2")
    print(f"  across-date mean alpha: {statistics.fmean(alpha_series):+.3f}pp  "
          f"t={t_alpha:+.2f}")
    return t_hit, len(dates)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--symbols', type=int, default=None)
    args = ap.parse_args()
    print("Part B — verdict replay (advise() unchanged), alpha label, "
          "non-overlapping windows.")
    print("Survivorship caveat: current constituents -> biased TOWARD skill; "
          "a null is STRONGER than it looks.")
    for H in (10, 30):
        per_date, n_reb = replay(H, args.symbols)
        summarise(per_date, H, n_reb)
    return 0


if __name__ == '__main__':
    sys.exit(main())
