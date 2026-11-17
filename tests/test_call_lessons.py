import call_lessons as cl


def _calls(n, key, correct_n, src='advice'):
    out = []
    for i in range(n):
        c = {'source': src, 'account': 'primary', 'run_date': '2026-10-01', 'symbol': f'{key}{i}',
             'attrs': {'verdict': 'SELL', 'regime': key, 'trigger_type': 'MACRO', 'confidence': 72, 'quality': None,
                       'sector_group': 'X', 'sector_rule': None, 'rotation_reason': None, 'thesis_action': None, 'weekday': 'Mon'}}
        out.append((c, {'at': {20: 5.0 if i < correct_n else -5.0}, 'days': 25}))
    return out


def test_one_true_lesson_one_blocked_by_haircut():
    base = _calls(200, 'BULL', 120) + _calls(40, 'CHOP', 10) + _calls(31, 'PANIC', 14)   # 60% / 25% / 45%
    res = cl.find_lessons([c for c, _ in base], {cl.key(c): s for c, s in base})
    pop = res['populations']['holdings']
    assert pop['overall']['n'] == 271 and pop['buckets_scanned'] > 1
    firing = {(l['bucket'], l['key']) for l in pop['lessons']}
    assert ('verdict_regime', 'SELL×CHOP') in firing and ('verdict_regime', 'SELL×PANIC') not in firing
    assert pop['lessons'][0]['text'] == 'SELL calls in CHOP: 10 of 40 right (25%) vs 53% overall, p=0.000'


def test_haircut_blocks_a_small_scan():
    base = _calls(30, 'CHOP', 5)                       # 17% but it IS the population
    res = cl.find_lessons([c for c, _ in base], {cl.key(c): s for c, s in base})
    assert res['populations']['holdings']['lessons'] == []


def test_gap_without_significance_does_not_fire():
    # CHOP 11/30 (37%) vs 51/90 (57%) overall: 20pp gap, but one-sided p ~0.02 > 0.05/6
    base = _calls(60, 'BULL', 40) + _calls(30, 'CHOP', 11)
    res = cl.find_lessons([c for c, _ in base], {cl.key(c): s for c, s in base})
    assert res['populations']['holdings']['lessons'] == []


def test_lesson_clears_after_two_misses():
    prior = {'populations': {'holdings': {'lessons': [{'bucket': 'verdict_regime', 'key': 'SELL×CHOP', 'since': '2026-11-01',
                                                       'text': 'old'}], 'clearing': []}}}
    base = _calls(200, 'BULL', 120) + _calls(40, 'CHOP', 25)
    r1 = cl.find_lessons([c for c, _ in base], {cl.key(c): s for c, s in base}, prior)
    assert r1['populations']['holdings']['clearing'][0]['key'] == 'SELL×CHOP' and r1['cleared'] == []
    r2 = cl.find_lessons([c for c, _ in base], {cl.key(c): s for c, s in base}, r1)
    assert r2['populations']['holdings']['clearing'] == [] and r2['populations']['holdings']['lessons'] == []
    assert r2['cleared'] == ['old']


def test_universe_is_its_own_population_and_targets_only_in_scorecard():
    base = _calls(40, 'CHOP', 10, src='universe') + _calls(40, 'BULL', 30, src='target')
    calls, scores = [c for c, _ in base], {cl.key(c): s for c, s in base}
    res = cl.find_lessons(calls, scores)
    assert res['populations']['universe']['overall']['n'] == 40 and res['populations']['holdings']['overall']['n'] == 0
    sc = cl.scorecard(calls, scores)
    cell = sc['by_source']['target'][20]
    assert {k: cell[k] for k in ('n', 'hit_pct', 'mean_alpha_pct')} == {'n': 40, 'hit_pct': 75.0, 'mean_alpha_pct': 2.5}


def test_brief_line_on_new_or_cleared_lesson():
    import morning_brief as mb
    lessons = {'asof': '2026-11-15', 'new': ['SELL calls in CHOP: 10 of 40 right (25%) vs 53% overall, p=0.000'], 'cleared': []}
    lines = mb.compose('2026-11-16', [], [], None, None, {}, None, None, None, lessons)
    assert [l for l in lines if l['kind'] == 'lessons'][0]['text'] == 'New lesson: SELL calls in CHOP: 10 of 40 right (25%) vs 53% overall, p=0.000'
    old = dict(lessons, asof='2026-11-08')
    assert not [l for l in mb.compose('2026-11-16', [], [], None, None, {}, None, None, None, old) if l['kind'] == 'lessons']


def test_bootstrap_degenerate_on_single_date():
    assert cl.block_bootstrap({'2026-11-10': [1.0, 1.0, 1.0]}, 'mean') == (1.0, 1.0)
    assert cl.block_bootstrap({}, 'mean') == (None, None)
    assert cl.block_bootstrap({'a': [1.0, -1.0]}, 'hit') == (50.0, 50.0)
    lo, hi = cl.block_bootstrap({'a': [0.0], 'b': [10.0], 'c': [20.0], 'd': [30.0]}, 'mean', n=200)
    assert lo < 15.0 < hi and lo >= 0.0 and hi <= 30.0


def test_scorecard_carries_ci_per_benchmark():
    base = _calls(20, 'BULL', 10)
    for i, (c, s) in enumerate(base):
        c['run_date'] = f'2026-10-{1 + i % 5:02d}'
        s['at_sector'] = {20: 1.0 if i % 2 else -3.0}
        s['at_size'] = {20: None}
    calls, scores = [c for c, _ in base], {cl.key(c): s for c, s in base}
    cell = cl.scorecard(calls, scores)['by_source']['advice'][20]
    assert cell['n'] == 20 and cell['hit_pct'] == 50.0 and cell['mean_alpha_pct'] == 0.0
    assert len(cell['hit_ci']) == 2 and cell['hit_ci'][0] <= 50.0 <= cell['hit_ci'][1]
    assert len(cell['alpha_ci']) == 2
    assert cell['sector'] == {'n': 20, 'hit_pct': 50.0, 'mean_alpha_pct': -1.0,
                              'hit_ci': cell['sector']['hit_ci'], 'alpha_ci': cell['sector']['alpha_ci']}
    assert cell['size'] is None


def test_bootstrap_is_fast_at_universe_scale():
    import time
    by_date = {f'd{i}': [float(j % 7 - 3) for j in range(500)] for i in range(55)}
    t0 = time.time()
    lo, hi = cl.block_bootstrap(by_date, 'mean')
    assert time.time() - t0 < 0.5, 'one cell must cost well under a second'
    assert lo <= hi and abs(lo) < 1.0           # every date identical -> a tight interval around the mean


def test_compute_writes_tax_cost_curve():
    import json
    from unittest.mock import patch
    from tests.test_call_outcomes import _fake
    f = _fake()
    f.tables['call_outcomes'] = [{'source': 'advice', 'account': 'primary', 'run_date': '2026-11-10', 'symbol': 'AAA',
                                  'td': 5, 'date': '2026-11-17', 'ret_pct': -3.0,
                                  'signed_alpha_pct': 1.0, 'mae_pct': 0, 'mfe_pct': 0}]
    with patch.object(cl.db, 'supabase', f):
        cl.compute()
    row = next(r for r in f.tables['app_config'] if r['key'] == 'tax_cost_curve')
    assert json.loads(row['value']) == {'5': {'n': 1, 'mean_ret_pct': -3.0}}
