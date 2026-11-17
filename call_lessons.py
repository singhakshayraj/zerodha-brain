"""[P-60] Lessons: pre-registered day-of-call attributes whose calls are right
less often than the rest, at trading day 20. Reported only, never acted on.

A lesson fires when a bucket has n >= MIN_N scored calls, its hit rate is at
least GAP_PP points under the population's, and the one-sided binomial p-value
against the population rate is <= ALPHA / buckets scanned (Bonferroni). The
same test upward is a strength. A lesson clears after two consecutive misses.
Adding a bucket is a spec change (docs/superpowers/specs/2026-11-16-call-evaluation-design.md).
"""
import json
from datetime import date

from scipy.stats import binomtest

import database as db
from call_scoring import CHECKPOINTS

BUCKETS = ('verdict_regime', 'trigger_type', 'confidence_band', 'sector_group', 'thesis_agrees',
           'sector_rule', 'quality_band', 'rotation_reason', 'weekday')
CHECKPOINT = 20
MIN_N = 30
GAP_PP = 15
ALPHA = 0.05
POPULATION = {'advice': 'holdings', 'natwar': 'holdings', 'natwar_v1': 'holdings', 'universe': 'universe'}
SELLS = ('SELL', 'SELL_ON_BOUNCE', 'TRIM')


def key(call: dict) -> tuple:
    return tuple(str(call[k]) for k in ('source', 'account', 'run_date', 'symbol'))


def _band(v, cuts):
    if v is None:
        return None
    lo, hi = cuts
    return f'<{lo}' if v < lo else f'{lo}–{hi - 1}' if v < hi else f'≥{hi}'


def bucket_key(name: str, call: dict):
    a = call['attrs']
    if name == 'verdict_regime':
        return f"{a['verdict']}×{a['regime']}" if a.get('regime') else None
    if name == 'confidence_band':
        return _band(a.get('confidence'), (50, 70))
    if name == 'quality_band':
        return _band(a.get('quality'), (40, 70))
    if name == 'thesis_agrees':
        t = a.get('thesis_action')
        if not t:
            return None
        sell = a['verdict'] in SELLS
        return 'agree' if (sell and t in ('EXIT', 'TRIM')) or (not sell and t in ('HOLD', 'ADD')) else 'disagree'
    v = a.get(name)
    return str(v) if v not in (None, '') else None


def _label(bucket: str, k: str) -> str:
    if bucket == 'verdict_regime':
        v, r = k.split('×', 1)
        return f'{v} calls in {r}'
    return {'trigger_type': f'{k} trigger calls', 'confidence_band': f'Confidence {k} calls',
            'sector_group': f'{k} calls', 'sector_rule': f'Calls with sector rule {k}',
            'quality_band': f'Quality {k} calls', 'rotation_reason': f'Switches ({k})', 'weekday': f'{k} calls',
            'thesis_agrees': 'Calls the thesis agreed with' if k == 'agree' else 'Calls the thesis disagreed with'}[bucket]


def _population(calls, scores, prior_pop, asof):
    scored = [(c, scores[key(c)]['at'][CHECKPOINT]) for c in calls
              if key(c) in scores and scores[key(c)]['at'].get(CHECKPOINT) is not None]
    n = len(scored)
    hits = sum(a > 0 for _, a in scored)
    overall = {'n': n, 'hit_pct': round(100 * hits / n, 1) if n else None,
               'mean_alpha_pct': round(sum(a for _, a in scored) / n, 2) if n else None}
    groups = {}
    for c, a in scored:
        for b in BUCKETS:
            k = bucket_key(b, c)
            if k is not None:
                groups.setdefault((b, k), []).append(a)
    tested = {bk: v for bk, v in groups.items() if len(v) >= MIN_N}
    cut = ALPHA / max(len(tested), 1)
    rate = hits / n if n else 0
    prior_lessons = {(l['bucket'], l['key']): l for l in (prior_pop or {}).get('lessons', [])}
    prior_clearing = {(l['bucket'], l['key']): l for l in (prior_pop or {}).get('clearing', [])}
    lessons, strengths = [], []
    for (b, k), vals in sorted(tested.items()):
        m, right = len(vals), sum(a > 0 for a in vals)
        hit = 100 * right / m
        row = {'bucket': b, 'key': k, 'n': m, 'hit_pct': round(hit, 1), 'overall_hit_pct': overall['hit_pct'],
               'mean_alpha_pct': round(sum(vals) / m, 2)}
        for side, out, gap in (('less', lessons, overall['hit_pct'] - hit), ('greater', strengths, hit - overall['hit_pct'])):
            if gap < GAP_PP:
                continue
            p = binomtest(right, m, rate, alternative=side).pvalue
            if p <= cut:
                was = prior_lessons.get((b, k)) or prior_clearing.get((b, k)) or {}
                out.append(dict(row, p=round(p, 4), since=was.get('since', asof) if side == 'less' else asof,
                                text=f'{_label(b, k)}: {right} of {m} right ({hit:.0f}%) vs '
                                     f"{overall['hit_pct']:.0f}% overall, p={p:.3f}"))
    firing = {(l['bucket'], l['key']) for l in lessons}
    clearing = [dict(l, misses=1) for bk, l in prior_lessons.items() if bk not in firing]
    cleared = [l for bk, l in prior_clearing.items() if bk not in firing]
    lessons.sort(key=lambda l: l['p'])
    return ({'overall': overall, 'buckets_scanned': len(tested), 'haircut': round(cut, 5),
             'lessons': lessons, 'strengths': strengths, 'clearing': clearing},
            [l['text'] for l in lessons if (l['bucket'], l['key']) not in prior_lessons and (l['bucket'], l['key']) not in prior_clearing],
            [l.get('text', l['key']) for l in cleared])


def find_lessons(calls: list, scores: dict, prior: dict = None, asof: str = None) -> dict:
    asof = asof or date.today().isoformat()
    out = {'asof': asof, 'checkpoint': CHECKPOINT, 'populations': {}, 'new': [], 'cleared': []}
    for pop in ('holdings', 'universe'):
        members = [c for c in calls if POPULATION.get(c['source']) == pop]
        res, new, gone = _population(members, scores, ((prior or {}).get('populations') or {}).get(pop), asof)
        out['populations'][pop] = res
        out['new'] += new
        out['cleared'] += gone
    return out


BOOT_N = 1000
BOOT_SEED = 0


def block_bootstrap(values_by_date: dict, stat: str = 'mean', n: int = BOOT_N, seed: int = BOOT_SEED):
    """95% percentile interval of `stat` ('mean' of the values, or 'hit' =
    % > 0) over the pooled values, resampling whole dates with replacement
    (calls made the same day are not independent). Works on per-date
    (sum, hits, n) aggregates, so a draw costs O(dates), not O(values).
    One date, or all-identical values, gives a degenerate (v, v)."""
    import numpy as np
    dates = [d for d, v in values_by_date.items() if len(v)]
    if not dates:
        return (None, None)
    arr = [np.asarray(values_by_date[d], dtype=float) for d in dates]
    sums = np.array([a.sum() for a in arr])
    hits = np.array([(a > 0).sum() for a in arr], dtype=float)
    cnt = np.array([a.size for a in arr], dtype=float)
    num = hits * 100 if stat == 'hit' else sums
    rng = np.random.default_rng(seed)
    picks = rng.integers(0, len(dates), (n, len(dates)))
    stats = num[picks].sum(axis=1) / cnt[picks].sum(axis=1)
    return (round(float(np.percentile(stats, 2.5)), 2), round(float(np.percentile(stats, 97.5)), 2))


def _cell(by_date: dict):
    v = [x for xs in by_date.values() for x in xs]
    if not v:
        return None
    return {'n': len(v), 'hit_pct': round(100 * sum(a > 0 for a in v) / len(v), 1),
            'mean_alpha_pct': round(sum(v) / len(v), 2),
            'hit_ci': list(block_bootstrap(by_date, 'hit')), 'alpha_ci': list(block_bootstrap(by_date, 'mean'))}


def scorecard(calls: list, scores: dict) -> dict:
    """by_source[src][cp] = nifty cell {n, hit_pct, mean_alpha_pct, hit_ci, alpha_ci}
    plus 'sector' / 'size' sub-cells of the same shape (None when no call has
    that cohort). Nifty stays top-level so existing readers keep working."""
    by = {}
    for c in calls:
        s = scores.get(key(c))
        if not s:
            continue
        for cp in CHECKPOINTS:
            if s['at'].get(cp) is None:
                continue
            slot = by.setdefault(c['source'], {}).setdefault(cp, {'nifty': {}, 'sector': {}, 'size': {}})
            for bench, src in (('nifty', 'at'), ('sector', 'at_sector'), ('size', 'at_size')):
                a = (s.get(src) or {}).get(cp)
                if a is not None:
                    slot[bench].setdefault(c['run_date'], []).append(a)
    out = {}
    for src, cps in by.items():
        out[src] = {}
        for cp, slot in cps.items():
            cell = _cell(slot['nifty'])
            cell['sector'], cell['size'] = _cell(slot['sector']), _cell(slot['size'])
            out[src][cp] = cell
    return {'asof': date.today().isoformat(), 'by_source': out}


def compute() -> dict:
    import call_outcomes
    calls = call_outcomes.load_calls(call_outcomes.BARS_START)
    scores = call_outcomes.latest_summary(calls)
    raw = db.get_config('call_lessons_latest')
    res = find_lessons(calls, scores, json.loads(raw) if raw else None)
    db.write_config('call_lessons_latest', json.dumps(res, default=str))
    db.write_config('call_scorecard_latest', json.dumps(scorecard(calls, scores), default=str))
    import tax_context
    db.write_config('tax_cost_curve', json.dumps(tax_context.cost_curve(
        call_outcomes._stored(min(c['run_date'] for c in calls)) if calls else {}, calls)))
    for pop, r in res['populations'].items():
        print(f"[lessons] {pop}: n={r['overall']['n']} at day {CHECKPOINT}, hit {r['overall']['hit_pct']}%, "
              f"{r['buckets_scanned']} buckets, {len(r['lessons'])} lessons")
        for l in r['lessons']:
            print(f"[lessons]   {l['text']}")
    return res
