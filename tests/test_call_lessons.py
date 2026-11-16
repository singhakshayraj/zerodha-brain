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
    assert sc['by_source']['target'][20] == {'n': 40, 'hit_pct': 75.0, 'mean_alpha_pct': 2.5}


def test_brief_line_on_new_or_cleared_lesson():
    import morning_brief as mb
    lessons = {'asof': '2026-11-15', 'new': ['SELL calls in CHOP: 10 of 40 right (25%) vs 53% overall, p=0.000'], 'cleared': []}
    lines = mb.compose('2026-11-16', [], [], None, None, {}, None, None, None, lessons)
    assert [l for l in lines if l['kind'] == 'lessons'][0]['text'] == 'New lesson: SELL calls in CHOP: 10 of 40 right (25%) vs 53% overall, p=0.000'
    old = dict(lessons, asof='2026-11-08')
    assert not [l for l in mb.compose('2026-11-16', [], [], None, None, {}, None, None, None, old) if l['kind'] == 'lessons']
