#!/usr/bin/env python3
"""Advisor scoring experiments: replay pre-registered weight variants over
cached daily history and rank them on out-of-sample evidence.

WHY this can exist at all: advisor_scoring.advise() is pure, and daily bars are
never revised. So any scoring variant can be re-run over any past date without
a token and without waiting for forward data to accumulate. The live track
record is 98 graded calls on 21 correlated holdings -- far too thin to choose
between variants. This replays ~500 names over ~5 years instead.

METHOD (deliberately conservative -- [P-35]/V-12 taught us how easy a false
positive is here):
  * unit of observation = (date, symbol); score vs forward alpha vs Nifty
  * per-DATE cross-sectional Spearman IC, then a t-test across dates
    (Fama-MacBeth). Pooling instead would treat 500 names moving together on
    one day as 500 independent facts -- they are roughly one.
  * non-overlapping forward windows (stride = horizon), so the IC series is
    not autocorrelated by construction
  * dates split chronologically into EXPLORE then HOLDOUT; a variant only
    counts as a winner if it survives the holdout it was not chosen on
  * Holm correction across variants, because testing 8 ideas at p<0.05 finds
    one "winner" from noise about a third of the time

WHAT IT CANNOT TEST: news sentiment (not reconstructable historically) and
your own tradebook history. Those terms are held at 0 for every variant, so
they cancel in the comparison rather than favouring any one of them.

Usage:
  python3 scripts/advisor_lab.py --selftest   # equivalence guard, no data needed
  python3 scripts/advisor_lab.py              # full run, prints leaderboard
  python3 scripts/advisor_lab.py --horizon 30 # MACRO horizon instead of MICRO
"""
import argparse
import json
import os
import pickle
import statistics
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from advisor_scoring import (MIN_DAILY_BARS, relative_strength, trend_consistency,
                             trend_score)
from indicators import run_all_indicators

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE = os.path.join(ROOT, 'data', 'daily_history.pkl')
RESULTS = os.path.join(ROOT, 'data', 'advisor_lab_results.jsonl')
BENCHMARK = 'NSE:NIFTY 50'

# Production reads ~400 calendar days ≈ 271 trading bars. Scoring a 1300-bar
# prefix would seed the EMAs differently from live, so the replay would be
# measuring a scorer we do not run. Trail the same depth production sees.
PROD_WINDOW_BARS = 271
MIN_SYMBOLS_PER_DATE = 30
EXPLORE_FRACTION = 0.6

# Pre-registered. Fixed BEFORE looking at any result -- adding variants after
# seeing the leaderboard is how you fit noise and call it a finding.
BASELINE = {'ema200': 1.0, 'ema50': 1.0, 'consistency': 1.0, 'momentum': 1.0,
            'adx': 1.0, 'rel_strength': 1.0, 'momentum_lookback': 20,
            'adx_min': 20}


def _v(**kw):
    out = dict(BASELINE)
    out.update(kw)
    return out


VARIANTS = {
    'baseline':        _v(),
    'anchor_heavy':    _v(ema200=1.5, momentum=0.5),
    'momentum_heavy':  _v(momentum=1.5, ema200=0.5),
    'rs_heavy':        _v(rel_strength=1.5),
    'rs_off':          _v(rel_strength=0.0),
    'consistency_off': _v(consistency=0.0),
    'adx_strict':      _v(adx_min=25),
    'momentum_slow':   _v(momentum_lookback=60),
}


def variant_score(ind: dict, closes: list, consistency, rel_strength,
                  v: dict) -> int:
    """Parameterised mirror of advisor_scoring.trend_score.

    At BASELINE this MUST equal trend_score(..., news_sent=None, regime=None)
    exactly -- test_variant_scorer_matches_production is the guard. If it ever
    drifts, every number this script prints is about a scorer we do not ship.
    """
    price = ind.get('current_close') or 0
    # Production truncates EVERY term to int BEFORE summing, so a float
    # accumulator drifts by a point. Mirror it term by term, then apply the
    # variant multiplier on top of the production-shaped integer.
    score = 0
    ema200, ema50 = ind.get('ema_200'), ind.get('ema_50')
    if price and ema200:
        score += round((20 if price > ema200 else -20) * v['ema200'])
    if price and ema50:
        score += round((15 if price > ema50 else -15) * v['ema50'])

    if consistency is not None:
        base = int(max(-15, min(15, (consistency - 50) / 50 * 15)))
        score += round(base * v['consistency'])

    lb = v['momentum_lookback']
    if len(closes) >= lb + 1 and closes[-lb - 1]:
        mom = (closes[-1] - closes[-lb - 1]) / closes[-lb - 1] * 100
        base = int(max(-20, min(20, mom / 6 * 20)))
        score += round(base * v['momentum'])

    adx = ind.get('adx')
    if adx and adx >= v['adx_min']:
        plus, minus = ind.get('adx_plus_di') or 0, ind.get('adx_minus_di') or 0
        score += round((10 if plus > minus else -10) * v['adx'])

    if rel_strength is not None:
        base = int(max(-20, min(20, rel_strength / 10 * 20)))
        score += round(base * v['rel_strength'])

    return max(-100, min(100, int(score)))


def _bar_date(bar: dict) -> str:
    ts = bar.get('timestamp') or ''
    return ts[:10] if isinstance(ts, str) else str(ts)[:10]


def load_panel(horizon: int, stride: int, limit_symbols: int = None):
    """-> {date: [(symbol, {variant: score}, fwd_alpha_pct), ...]}

    Sampling runs on the BENCHMARK's date grid, not each symbol's own index.
    Symbols list on different days, so index-based striding would scatter them
    across different dates and leave too few names per date to compute a
    cross-sectional IC at all.
    """
    with open(CACHE, 'rb') as f:
        cache = pickle.load(f)
    bench_bars = cache.get(BENCHMARK) or []
    if not bench_bars:
        raise SystemExit(f"no benchmark series in {CACHE}; re-run the puller")
    bench_by_date = {_bar_date(b): float(b['close']) for b in bench_bars
                     if b.get('close') is not None}
    grid = sorted(bench_by_date)
    # Every observation must see a full production-depth window, so the replay
    # scores what production would have scored rather than a thinner series.
    sample_dates = set(grid[PROD_WINDOW_BARS:len(grid) - horizon:stride])
    print(f"benchmark grid: {len(grid)} days -> {len(sample_dates)} sample dates")

    symbols = sorted(s for s in cache if s != BENCHMARK)
    if limit_symbols:
        symbols = symbols[:limit_symbols]

    panel = {}
    for sym in symbols:
        bars = [b for b in (cache.get(sym) or [])
                if b.get('close') is not None and _bar_date(b) in bench_by_date]
        if len(bars) < PROD_WINDOW_BARS + horizon + 1:
            continue
        closes = [float(b['close']) for b in bars]
        dates = [_bar_date(b) for b in bars]
        bench = [bench_by_date[d] for d in dates]

        for i in range(PROD_WINDOW_BARS, len(bars) - horizon):
            if dates[i] not in sample_dates:
                continue
            lo = i - PROD_WINDOW_BARS + 1
            window, w_closes, w_bench = bars[lo:i + 1], closes[lo:i + 1], bench[lo:i + 1]
            ind = run_all_indicators(window)
            cons = trend_consistency(w_closes)
            rs = relative_strength(w_closes, w_bench)
            scores = {name: variant_score(ind, w_closes, cons, rs, v)
                      for name, v in VARIANTS.items()}
            stock_ret = (closes[i + horizon] / closes[i] - 1) * 100
            bench_ret = (bench[i + horizon] / bench[i] - 1) * 100
            panel.setdefault(dates[i], []).append(
                (sym, scores, stock_ret - bench_ret))
    return panel


def _spearman(xs, ys):
    """Rank correlation. scipy is present but this keeps the run dependency
    free and is exact enough for ties-light score data."""
    n = len(xs)
    if n < 3:
        return None

    def ranks(vals):
        order = sorted(range(n), key=lambda k: vals[k])
        r = [0.0] * n
        i = 0
        while i < n:
            j = i
            while j + 1 < n and vals[order[j + 1]] == vals[order[i]]:
                j += 1
            avg = (i + j) / 2 + 1
            for k in range(i, j + 1):
                r[order[k]] = avg
            i = j + 1
        return r

    rx, ry = ranks(xs), ranks(ys)
    mx, my = statistics.fmean(rx), statistics.fmean(ry)
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    dx = sum((a - mx) ** 2 for a in rx) ** 0.5
    dy = sum((b - my) ** 2 for b in ry) ** 0.5
    return None if dx == 0 or dy == 0 else num / (dx * dy)


def _t_and_p(series):
    n = len(series)
    if n < 3:
        return 0.0, 1.0
    m = statistics.fmean(series)
    sd = statistics.stdev(series)
    if sd == 0:
        return 0.0, 1.0
    t = m / (sd / n ** 0.5)
    try:
        from scipy import stats
        p = 2 * (1 - stats.t.cdf(abs(t), df=n - 1))
    except Exception:
        p = float('nan')
    return t, p


def _holm(pvals: dict) -> dict:
    items = sorted(pvals.items(), key=lambda kv: kv[1])
    m = len(items)
    out, prev = {}, 0.0
    for i, (k, p) in enumerate(items):
        adj = min(1.0, max(prev, (m - i) * p))
        out[k] = adj
        prev = adj
    return out


def evaluate(panel: dict, horizon: int):
    dates = sorted(d for d, rows in panel.items()
                   if len(rows) >= MIN_SYMBOLS_PER_DATE)
    if len(dates) < 8:
        raise SystemExit(f"only {len(dates)} usable dates -- not enough to test")
    split = int(len(dates) * EXPLORE_FRACTION)
    explore, holdout = dates[:split], dates[split:]

    ic = {name: {} for name in VARIANTS}
    spread = {name: {} for name in VARIANTS}
    for d in dates:
        rows = panel[d]
        alphas = [r[2] for r in rows]
        for name in VARIANTS:
            scores = [r[1][name] for r in rows]
            c = _spearman(scores, alphas)
            if c is not None:
                ic[name][d] = c
            k = max(1, len(rows) // 10)
            ordered = sorted(zip(scores, alphas), key=lambda t: t[0])
            bot = statistics.fmean([a for _, a in ordered[:k]])
            top = statistics.fmean([a for _, a in ordered[-k:]])
            spread[name][d] = top - bot

    def block(name, ds):
        vals = [ic[name][d] for d in ds if d in ic[name]]
        sp = [spread[name][d] for d in ds if d in spread[name]]
        t, p = _t_and_p(vals)
        return {'n_dates': len(vals),
                'mean_ic': statistics.fmean(vals) if vals else 0.0,
                't': t, 'p': p,
                'decile_spread_pct': statistics.fmean(sp) if sp else 0.0}

    res = {name: {'explore': block(name, explore), 'holdout': block(name, holdout)}
           for name in VARIANTS}
    holm = _holm({n: res[n]['holdout']['p'] for n in VARIANTS})
    for n in VARIANTS:
        res[n]['holdout']['p_holm'] = holm[n]
    return res, explore, holdout


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--horizon', type=int, default=10,
                    help='forward trading days (10 = MICRO, 30 = MACRO)')
    ap.add_argument('--symbols', type=int, default=None,
                    help='cap symbol count (for a quick run)')
    ap.add_argument('--selftest', action='store_true')
    args = ap.parse_args()

    if args.selftest:
        return selftest()

    stride = args.horizon          # non-overlapping forward windows
    print(f"horizon={args.horizon}d stride={stride} variants={len(VARIANTS)}")
    panel = load_panel(args.horizon, stride, args.symbols)
    res, explore, holdout = evaluate(panel, args.horizon)

    obs = sum(len(v) for v in panel.values())
    print(f"\n{obs} observations | {len(explore)} explore dates | "
          f"{len(holdout)} holdout dates\n")
    hdr = (f"{'variant':<17}{'expl IC':>9}{'expl t':>8}"
           f"{'hold IC':>9}{'hold t':>8}{'p_holm':>9}{'d10 spread':>12}")
    print(hdr)
    print('-' * len(hdr))
    for name in sorted(VARIANTS, key=lambda n: -res[n]['holdout']['mean_ic']):
        e, h = res[name]['explore'], res[name]['holdout']
        print(f"{name:<17}{e['mean_ic']:>9.4f}{e['t']:>8.2f}"
              f"{h['mean_ic']:>9.4f}{h['t']:>8.2f}{h['p_holm']:>9.3f}"
              f"{h['decile_spread_pct']:>11.2f}%")

    # Exploratory context, NOT a test: is the score decaying over time, or was
    # it never there? Both horizons show explore-positive / holdout-negative,
    # and the holdout is the recent end of the window.
    by_year = {}
    for d, rows in panel.items():
        if len(rows) < MIN_SYMBOLS_PER_DATE:
            continue
        c = _spearman([r[1]['baseline'] for r in rows], [r[2] for r in rows])
        if c is not None:
            by_year.setdefault(d[:4], []).append(c)
    print("\nbaseline IC by year (exploratory, not a test):")
    for y in sorted(by_year):
        vals = by_year[y]
        t, _ = _t_and_p(vals)
        print(f"  {y}  n={len(vals):>3}  mean IC {statistics.fmean(vals):+.4f}  t={t:+.2f}")

    base_h = res['baseline']['holdout']
    winners = [n for n in VARIANTS
               if n != 'baseline'
               and res[n]['holdout']['p_holm'] < 0.05
               and res[n]['holdout']['mean_ic'] > base_h['mean_ic']]
    print(f"\nbaseline holdout IC {base_h['mean_ic']:+.4f} "
          f"(t={base_h['t']:+.2f}, Holm p={base_h['p_holm']:.3f})")
    print("beats baseline in holdout after Holm: "
          + (', '.join(winners) if winners else "NONE"))

    row = {'run_at': datetime.now(timezone.utc).isoformat(),
           'horizon': args.horizon, 'observations': obs,
           'explore_dates': len(explore), 'holdout_dates': len(holdout),
           'variants': {n: VARIANTS[n] for n in VARIANTS},
           'results': res, 'winners': winners}
    with open(RESULTS, 'a') as f:
        f.write(json.dumps(row) + '\n')
    print(f"\nappended to {RESULTS}")
    return 0


def selftest() -> int:
    """The one check that matters: at BASELINE the lab's scorer must reproduce
    production trend_score bit for bit, or the leaderboard describes fiction."""
    import random
    random.seed(7)
    checked = 0
    for _ in range(400):
        closes = [100.0]
        for _ in range(300):
            closes.append(max(1.0, closes[-1] * (1 + random.gauss(0, 0.015))))
        bars = [{'open': c, 'high': c * 1.01, 'low': c * 0.99, 'close': c,
                 'volume': 10000,
                 'timestamp': '2025-01-01T00:00:00+0530'} for c in closes]
        ind = run_all_indicators(bars)
        cons = trend_consistency(closes)
        rs = random.choice([None, round(random.uniform(-15, 15), 2)])
        want = trend_score(ind, closes, consistency=cons, rel_strength=rs,
                           news_sent=None, regime=None)
        got = variant_score(ind, closes, cons, rs, VARIANTS['baseline'])
        assert got == want, f"drift: variant={got} production={want}"
        checked += 1
    print(f"selftest OK: {checked} random series, baseline == production "
          f"trend_score on every one")
    return 0


if __name__ == '__main__':
    sys.exit(main())
