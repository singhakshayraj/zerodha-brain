import numpy as np
from datetime import date, timedelta

import risk_targets as rt
from household_book import DEFAULT_TARGETS

CAPS = DEFAULT_TARGETS


def _walk(seed, n=80):
    r = np.random.RandomState(seed).normal(0, 0.01, n)
    return list(100 * np.cumprod(1 + r))


def _dated(vals):
    d0 = date(2026, 1, 1)
    return {(d0 + timedelta(days=i)).isoformat(): v for i, v in enumerate(vals)}


def _pos(s, q=10, p=100, cls='equity'):
    return {'symbol': s, 'quantity': q, 'last_price': p, 'asset_class': cls}


def test_caps_hold_and_sum_to_100():
    names = ['N%02d' % i for i in range(20)]
    groups = {s: (('S%d' % (i % 5)) if i % 5 < 4 else 'Outside Nifty 500', False)
              for i, s in enumerate(names)}   # 4 capped sectors + 1 uncapped bucket (4x20 < 100)
    raw = {s: 1.0 for s in names}
    raw['N00'] = 30.0
    for s in names:                      # skew one sector (S1) to ~50%
        if groups[s][0] == 'S1':
            raw[s] *= 8
    tot = sum(raw.values())
    raw = {s: v / tot for s, v in raw.items()}
    w, inf = rt.cap_weights(raw, groups, CAPS)
    assert inf == []
    assert abs(sum(w.values()) * 100 - 100) < 0.01
    assert all(v * 100 <= 8 + 1e-6 for v in w.values())
    for g in {'S0', 'S1', 'S2', 'S3'}:
        assert sum(v for s, v in w.items() if groups[s][0] == g) * 100 <= 20 + 1e-6


def test_infeasible_caps_named_not_enforced():
    groups = {s: ('S', False) for s in 'ABC'}
    w, inf = rt.cap_weights({'A': .5, 'B': .3, 'C': .2}, groups, CAPS)
    assert abs(sum(w.values()) - 1) < 1e-6 and 'name' in inf


def test_not_sector_bucket_uncapped():
    names = ['O%d' % i for i in range(14)] + ['P%d' % i for i in range(6)]
    groups = {s: ('Outside Nifty 500' if s[0] == 'O' else 'Real', False) for s in names}
    raw = {s: (3.0 if s[0] == 'O' else 1.0) for s in names}
    t = sum(raw.values())
    w, inf = rt.cap_weights({s: v / t for s, v in raw.items()}, groups, CAPS)
    assert sum(v for s, v in w.items() if s[0] == 'O') * 100 > 20


def test_etf_sleeve_capped_15():
    names = ['E1', 'E2', 'E3'] + ['N%02d' % i for i in range(20)]
    groups = {s: (('ETFs (gold, silver, index)', True) if s[0] == 'E' else ('S%d' % (int(s[1:]) % 5), False))
              for s in names}
    raw = {s: (6.0 if s[0] == 'E' else 1.0) for s in names}
    t = sum(raw.values())
    w, inf = rt.cap_weights({s: v / t for s, v in raw.items()}, groups, CAPS)
    assert sum(v for s, v in w.items() if s[0] == 'E') * 100 <= 15 + 1e-6
    assert abs(sum(w.values()) - 1) < 1e-6 and inf == []


def test_sell_qty_never_exceeds_held_or_goes_negative():
    assert rt.sell_qty_to_target(10, 100, 5.0, 8.0, 100000, 0.5) == 0
    assert rt.sell_qty_to_target(10, 100, 5.0, 5.0, 100000, 0.5) == 0
    assert rt.sell_qty_to_target(10, 100, 50.0, 0.0, 10000, 1.0) == 10
    assert rt.sell_qty_to_target(100, 100, 10.0, 6.0, 100000, 0.5) == 20


def test_build_two_identical_names_enb_one():
    a = _dated(_walk(1))
    out = rt.build([_pos('X'), _pos('Y')], {'X': a, 'Y': dict(a)}, {})
    assert abs(out['enb_now'] - 1.0) < 0.05 and out['below_floor'] is True
    assert out['enb_floor'] == 6 and {t['symbol'] for t in out['targets']} == {'X', 'Y'}


def test_build_too_few_names_returns_empty():
    out = rt.build([_pos('X')], {'X': _dated(_walk(1))}, {})
    assert out['targets'] == [] and out['enb_now'] is None
    out = rt.build([_pos('X'), _pos('Y')], {'X': _dated(_walk(1)), 'Y': _dated(_walk(2, 10))}, {})
    assert out['targets'] == [] and out['excluded'] == ['Y']


def test_step_halves_when_regime_drifted():
    n = 120
    base = np.random.RandomState(5).normal(0, 0.01, n)
    other = np.random.RandomState(6).normal(0, 0.01, n)
    px = lambda r: _dated(list(100 * np.cumprod(1 + r)))
    ps = [_pos('A1'), _pos('A2'), _pos('B1', 5), _pos('B2', 5)]
    secs = {'A1': 'Tech', 'A2': 'Tech', 'B1': 'Oil', 'B2': 'Oil'}
    # sleeve (Tech) vs rest: full-window corr low, recent corr high
    ra1 = np.where(np.arange(n) < n - 60, other, base)
    closes = {'A1': px(ra1), 'A2': px(ra1 * 0.9 + 0.001),
              'B1': px(np.where(np.arange(n) < n - 60, -other, base)),
              'B2': px(np.where(np.arange(n) < n - 60, np.roll(other, 3), base))}
    out = rt.build(ps, closes, secs)
    assert out['regime']['drifted'] is True and out['step'] == 0.25
    calm = rt.build(ps, {k: px(np.random.RandomState(i).normal(0, .01, n))
                         for i, k in enumerate(closes)}, secs)
    assert calm['step'] == 0.5


def test_only_infeasible_cap_is_dropped():
    names = ['N%02d' % i for i in range(20)]
    groups = {s: ('S%d' % (i % 4), False) for i, s in enumerate(names)}
    raw = {s: (30.0 if s == 'N00' else 1.0) for s in names}
    t = sum(raw.values())
    w, inf = rt.cap_weights({s: v / t for s, v in raw.items()}, groups, CAPS)
    assert inf == ['sector'] and abs(sum(w.values()) - 1) < 1e-6
    assert all(v * 100 <= 8 + 1e-6 for v in w.values())


def test_build_excludes_only_unpriced_name():
    a = _dated(_walk(1))
    ps = [_pos('X'), _pos('Y'), _pos('Z', p=0)]
    out = rt.build(ps, {'X': a, 'Y': _dated(_walk(2)), 'Z': dict(a)}, {})
    assert out['excluded'] == ['Z'] and {t['symbol'] for t in out['targets']} == {'X', 'Y'}
