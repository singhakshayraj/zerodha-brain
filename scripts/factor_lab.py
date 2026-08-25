#!/usr/bin/env python3
"""Standard cross-sectional equity factors, built and tested locally.

WHY: advisor_lab showed that reweighting the advisor's seven hand-picked
factors changes nothing (Holm p=1.000 across 8 variants, IC range 0.004).
They are one family -- every one is a trend/EMA read of the same price
series -- so no weighting of them adds information. This tests a DIFFERENT
family: the standard cross-sectional factor set from the equity literature.

Two structural differences from the current advisor, and they matter more
than any weight:

  1. CROSS-SECTIONAL standardisation. trend_score is absolute: "price above
     EMA200" scores +20 whether the whole market is above its EMA200 or
     nothing is. A z-score answers the question that actually predicts
     relative returns -- strong COMPARED TO WHAT ELSE I COULD HOLD today.

  2. Horizon. The advisor's momentum term is 20 days, which in the
     literature is short-term REVERSAL territory (Jegadeesh 1990), not
     momentum. The robust anomaly is 12-1: the 12-month return skipping the
     most recent month (Jegadeesh & Titman 1993).

Factors are PRE-REGISTERED from published work, listed before any result was
seen. Multiplicity is handled by Holm; selection-on-explore composites are
scored only on the holdout they were not chosen on.

Usage:
  python3 scripts/factor_lab.py --selftest
  python3 scripts/factor_lab.py                # 10d horizon
  python3 scripts/factor_lab.py --horizon 30
  python3 scripts/factor_lab.py --sector-neutral
"""
import argparse
import csv
import json
import math
import os
import pickle
import statistics
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Same statistics the advisor lab uses -- one implementation, so a fix to the
# IC machinery cannot silently apply to one lab and not the other.
from advisor_lab import _holm, _spearman, _t_and_p

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE = os.path.join(ROOT, 'data', 'daily_history.pkl')
UNIVERSE = os.path.join(ROOT, 'data', 'nifty500.csv')
RESULTS = os.path.join(ROOT, 'data', 'factor_lab_results.jsonl')
BENCHMARK = 'NSE:NIFTY 50'

WARMUP = 273            # 252d lookback + 21d skip, the deepest factor needs it
MIN_SYMBOLS_PER_DATE = 50
EXPLORE_FRACTION = 0.6
WINSOR = 3.0            # z-score clip, standard practice
SELECT_T = 2.0          # explore |t| a factor must clear to enter combo_explore


def _ret(a, b):
    return (a / b - 1.0) if b else None


def _std(xs):
    return statistics.stdev(xs) if len(xs) > 1 else None


def _skew(xs):
    n = len(xs)
    if n < 8:
        return None
    m = statistics.fmean(xs)
    sd = statistics.pstdev(xs)
    if not sd:
        return None
    return sum(((x - m) / sd) ** 3 for x in xs) / n


def _beta_resid(rets, brets):
    """OLS beta vs the benchmark and the residual standard deviation."""
    n = min(len(rets), len(brets))
    if n < 30:
        return None, None
    r, b = rets[-n:], brets[-n:]
    mb = statistics.fmean(b)
    var = sum((x - mb) ** 2 for x in b)
    if not var:
        return None, None
    mr = statistics.fmean(r)
    cov = sum((x - mb) * (y - mr) for x, y in zip(b, r))
    beta = cov / var
    alpha = mr - beta * mb
    resid = [y - (alpha + beta * x) for x, y in zip(b, r)]
    return beta, _std(resid)


def compute_factors(i, closes, highs, vols, rets, brets):
    """All factors at bar index i, signed so HIGHER = higher expected alpha."""
    f = {}
    f['mom_12_1'] = _ret(closes[i - 21], closes[i - 252])
    f['mom_6_1'] = _ret(closes[i - 21], closes[i - 126])
    r1m = _ret(closes[i], closes[i - 21])
    f['rev_1m'] = -r1m if r1m is not None else None
    hi52 = max(highs[i - 251:i + 1])
    f['prox_52w'] = (closes[i] / hi52) if hi52 else None

    w252 = rets[i - 251:i + 1]
    w126 = rets[i - 125:i + 1]
    w21 = rets[i - 20:i + 1]
    sd252 = _std(w252)
    f['low_vol'] = -sd252 if sd252 is not None else None
    beta, ivol = _beta_resid(w126, brets[i - 125:i + 1])
    f['low_beta'] = -beta if beta is not None else None
    f['low_ivol'] = -ivol if ivol is not None else None
    f['low_max'] = -max(w21) if w21 else None
    sk = _skew(w126)
    f['low_skew'] = -sk if sk is not None else None

    # Amihud illiquidity: |return| per rupee traded. Illiquid names carry a
    # premium; it is also the factor most likely to be untradeable for us,
    # which is worth knowing separately rather than hidden inside a blend.
    vals = [abs(rets[j]) / (closes[j] * vols[j])
            for j in range(i - 20, i + 1) if vols[j] and closes[j]]
    f['amihud'] = statistics.fmean(vals) * 1e9 if vals else None

    # Time-series momentum -- the closest thing to what the advisor already
    # does, kept as a control so the comparison is like-for-like.
    k = 2 / 201
    ema = closes[i - 251]
    for c in closes[i - 250:i + 1]:
        ema = c * k + ema * (1 - k)
    f['trend_ma'] = _ret(closes[i], ema)

    v21 = [v for v in vols[i - 20:i + 1] if v]
    v126 = [v for v in vols[i - 125:i + 1] if v]
    f['vol_growth'] = (statistics.fmean(v21) / statistics.fmean(v126) - 1
                       if v21 and v126 and statistics.fmean(v126) else None)
    return f


FACTORS = ['mom_12_1', 'mom_6_1', 'rev_1m', 'prox_52w', 'low_vol', 'low_beta',
           'low_ivol', 'low_max', 'low_skew', 'amihud', 'trend_ma',
           'vol_growth']


def zscore(vals: dict, winsor: float = WINSOR) -> dict:
    """Cross-sectional z, winsorised. Ranking is relative to the OTHER names
    available that day, which is the question a portfolio actually asks."""
    xs = [v for v in vals.values() if v is not None]
    if len(xs) < 10:
        return {}
    m, sd = statistics.fmean(xs), statistics.pstdev(xs)
    if not sd:
        return {}
    return {k: max(-winsor, min(winsor, (v - m) / sd))
            for k, v in vals.items() if v is not None}


def sector_neutralise(z: dict, sectors: dict) -> dict:
    """Demean within sector: a factor should not just be a bet on banks."""
    groups = {}
    for sym, v in z.items():
        groups.setdefault(sectors.get(sym, '?'), []).append(v)
    means = {s: statistics.fmean(v) for s, v in groups.items() if v}
    return {sym: v - means.get(sectors.get(sym, '?'), 0.0)
            for sym, v in z.items()}


def build_panel(horizon: int, stride: int, sector_neutral: bool):
    with open(CACHE, 'rb') as fh:
        cache = pickle.load(fh)
    with open(UNIVERSE) as fh:
        sectors = {r['symbol']: r['sector'] for r in csv.DictReader(fh)}

    bench = cache.get(BENCHMARK) or []
    bench_by_date = {b['timestamp'][:10]: float(b['close']) for b in bench
                     if b.get('close') is not None}
    grid = sorted(bench_by_date)
    bcloses = [bench_by_date[d] for d in grid]
    brets_all = [0.0] + [_ret(bcloses[i], bcloses[i - 1]) or 0.0
                         for i in range(1, len(bcloses))]
    bidx = {d: i for i, d in enumerate(grid)}
    sample_dates = set(grid[WARMUP:len(grid) - horizon:stride])
    print(f"grid {len(grid)} days -> {len(sample_dates)} sample dates "
          f"| sector_neutral={sector_neutral}")

    raw = {}     # date -> {symbol: {factor: value}}
    fwd = {}     # date -> {symbol: forward alpha %}
    for sym in sorted(s for s in cache if s != BENCHMARK):
        bars = [b for b in (cache.get(sym) or [])
                if b.get('close') is not None and b['timestamp'][:10] in bench_by_date]
        if len(bars) < WARMUP + horizon + 2:
            continue
        dates = [b['timestamp'][:10] for b in bars]
        closes = [float(b['close']) for b in bars]
        highs = [float(b.get('high') or b['close']) for b in bars]
        vols = [float(b.get('volume') or 0) for b in bars]
        rets = [0.0] + [_ret(closes[i], closes[i - 1]) or 0.0
                        for i in range(1, len(closes))]
        brets = [brets_all[bidx[d]] for d in dates]

        for i in range(WARMUP, len(bars) - horizon):
            d = dates[i]
            if d not in sample_dates:
                continue
            f = compute_factors(i, closes, highs, vols, rets, brets)
            raw.setdefault(d, {})[sym] = f
            sr = _ret(closes[i + horizon], closes[i])
            j = bidx[d]
            br = _ret(bcloses[j + horizon], bcloses[j]) if j + horizon < len(bcloses) else None
            if sr is not None and br is not None:
                fwd.setdefault(d, {})[sym] = (sr - br) * 100

    panel = {}
    for d, per_sym in raw.items():
        if len(per_sym) < MIN_SYMBOLS_PER_DATE:
            continue
        zs = {}
        for name in FACTORS:
            z = zscore({s: v.get(name) for s, v in per_sym.items()})
            if sector_neutral and z:
                z = sector_neutralise(z, sectors)
            zs[name] = z
        panel[d] = {'z': zs, 'fwd': fwd.get(d, {})}
    return panel


def _ic_series(panel, score_fn, dates):
    out = {}
    for d in dates:
        blk = panel[d]
        pairs = []
        for sym, a in blk['fwd'].items():
            s = score_fn(blk['z'], sym)
            if s is not None:
                pairs.append((s, a))
        if len(pairs) >= MIN_SYMBOLS_PER_DATE:
            c = _spearman([p[0] for p in pairs], [p[1] for p in pairs])
            if c is not None:
                out[d] = (c, pairs)
    return out


def _quintile_spread(pairs):
    k = max(1, len(pairs) // 5)
    o = sorted(pairs, key=lambda p: p[0])
    return statistics.fmean([a for _, a in o[-k:]]) - statistics.fmean([a for _, a in o[:k]])


def evaluate(panel, horizon):
    dates = sorted(panel)
    split = int(len(dates) * EXPLORE_FRACTION)
    explore, holdout = dates[:split], dates[split:]

    def single(name):
        return lambda z, sym: z[name].get(sym)

    rows = {}
    for name in FACTORS:
        rows[name] = (single(name), None)

    # Composite 1: equal-weight all factors. No fitting at all, which is the
    # most honest blend -- optimised weights are what advisor_lab just showed
    # buys nothing.
    def combo_ew(z, sym):
        vals = [z[n][sym] for n in FACTORS if sym in z[n]]
        return statistics.fmean(vals) if len(vals) >= len(FACTORS) // 2 else None

    rows['combo_ew'] = (combo_ew, None)

    results = {}
    ics = {}
    for name, (fn, _) in rows.items():
        e = _ic_series(panel, fn, explore)
        h = _ic_series(panel, fn, holdout)
        ics[name] = (e, h)
        te, _ = _t_and_p([v[0] for v in e.values()])
        th, ph = _t_and_p([v[0] for v in h.values()])
        results[name] = {
            'explore': {'n': len(e),
                        'mean_ic': statistics.fmean([v[0] for v in e.values()]) if e else 0.0,
                        't': te},
            'holdout': {'n': len(h),
                        'mean_ic': statistics.fmean([v[0] for v in h.values()]) if h else 0.0,
                        't': th, 'p': ph,
                        'q_spread': statistics.fmean(
                            [_quintile_spread(v[1]) for v in h.values()]) if h else 0.0},
        }

    # Composite 2: select on EXPLORE only, score on HOLDOUT. This is the
    # honest version of "keep what works" -- the selection never sees the
    # data it is judged on.
    chosen = [n for n in FACTORS if abs(results[n]['explore']['t']) >= SELECT_T]
    signed = {n: (1.0 if results[n]['explore']['mean_ic'] >= 0 else -1.0)
              for n in chosen}
    if chosen:
        def combo_sel(z, sym):
            vals = [signed[n] * z[n][sym] for n in chosen if sym in z[n]]
            return statistics.fmean(vals) if len(vals) >= max(1, len(chosen) // 2) else None
        h = _ic_series(panel, combo_sel, holdout)
        th, ph = _t_and_p([v[0] for v in h.values()])
        results['combo_explore_sel'] = {
            'explore': {'n': 0, 'mean_ic': 0.0, 't': 0.0, 'note': 'selection set'},
            'holdout': {'n': len(h),
                        'mean_ic': statistics.fmean([v[0] for v in h.values()]) if h else 0.0,
                        't': th, 'p': ph,
                        'q_spread': statistics.fmean(
                            [_quintile_spread(v[1]) for v in h.values()]) if h else 0.0},
            'selected': chosen}

    holm = _holm({n: results[n]['holdout'].get('p', 1.0) for n in results})
    for n in results:
        results[n]['holdout']['p_holm'] = holm[n]
    return results, explore, holdout, chosen


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--horizon', type=int, default=10)
    ap.add_argument('--sector-neutral', action='store_true')
    ap.add_argument('--selftest', action='store_true')
    args = ap.parse_args()
    if args.selftest:
        return selftest()

    panel = build_panel(args.horizon, args.horizon, args.sector_neutral)
    results, explore, holdout, chosen = evaluate(panel, args.horizon)

    print(f"\n{len(explore)} explore dates | {len(holdout)} holdout dates\n")
    hdr = (f"{'factor':<20}{'expl IC':>9}{'expl t':>8}{'hold IC':>9}"
           f"{'hold t':>8}{'p_holm':>9}{'Q5-Q1':>9}")
    print(hdr)
    print('-' * len(hdr))
    for name in sorted(results, key=lambda n: -results[n]['holdout']['mean_ic']):
        e, h = results[name]['explore'], results[name]['holdout']
        star = ' *' if h['p_holm'] < 0.05 else ''
        print(f"{name:<20}{e['mean_ic']:>9.4f}{e['t']:>8.2f}{h['mean_ic']:>9.4f}"
              f"{h['t']:>8.2f}{h['p_holm']:>9.3f}{h['q_spread']:>8.2f}%{star}")

    print(f"\nselected on explore (|t|>={SELECT_T}): "
          + (', '.join(chosen) if chosen else 'none'))
    survivors = [n for n in results
                 if results[n]['holdout']['p_holm'] < 0.05
                 and abs(results[n]['holdout']['mean_ic']) > 0]
    print("survive Holm in holdout: " + (', '.join(survivors) if survivors else 'NONE'))

    with open(RESULTS, 'a') as fh:
        fh.write(json.dumps({
            'run_at': datetime.now(timezone.utc).isoformat(),
            'horizon': args.horizon, 'sector_neutral': args.sector_neutral,
            'explore_dates': len(explore), 'holdout_dates': len(holdout),
            'selected_on_explore': chosen, 'survivors': survivors,
            'results': results}) + '\n')
    print(f"\nappended to {RESULTS}")
    return 0


def selftest():
    z = zscore({'a': 1.0, 'b': 1.0, 'c': 1.0, 'd': 1.0, 'e': 1.0,
                'f': 1.0, 'g': 1.0, 'h': 1.0, 'i': 1.0, 'j': 1.0})
    assert z == {}, 'zero-variance input must not produce z-scores'

    vals = {f's{i}': float(i) for i in range(100)}
    z = zscore(vals)
    assert abs(statistics.fmean(z.values())) < 1e-9, 'z must be mean-zero'
    assert max(z.values()) <= WINSOR and min(z.values()) >= -WINSOR, 'winsor'

    secs = {f's{i}': ('A' if i % 2 else 'B') for i in range(100)}
    sn = sector_neutralise(z, secs)
    for s in ('A', 'B'):
        grp = [v for k, v in sn.items() if secs[k] == s]
        assert abs(statistics.fmean(grp)) < 1e-9, f'sector {s} must be demeaned'

    # 12-1 momentum must read the t-252..t-21 window and IGNORE the last month.
    n = 400
    closes = [100.0] * n
    for i in range(n - 21, n):
        closes[i] = 1000.0                      # a huge last-month spike
    highs, vols = list(closes), [1e6] * n
    rets = [0.0] * n
    got = compute_factors(n - 1, closes, highs, vols, rets, rets)
    assert abs(got['mom_12_1']) < 1e-9, f"12-1 leaked the skip month: {got['mom_12_1']}"
    assert got['rev_1m'] < 0, 'reversal must be negative after a spike'

    b, iv = _beta_resid([0.01, -0.02, 0.03] * 20, [0.01, -0.02, 0.03] * 20)
    assert abs(b - 1.0) < 1e-9 and iv is not None and iv < 1e-9, 'self-beta = 1'
    print('selftest OK: z-score, winsor, sector demean, 12-1 skip window, beta')
    return 0


if __name__ == '__main__':
    sys.exit(main())
