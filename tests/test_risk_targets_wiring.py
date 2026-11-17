"""[P-68 ph2] risk pass is shadow: sell-grade sizing only, never moves a verdict, never fatal."""
import copy
import json
from unittest.mock import patch

import numpy as np

import portfolio_advisor as pa

DATES = [f'2026-{1 + i // 28:02d}-{1 + i % 28:02d}' for i in range(120)]


def _closes():
    rng = np.random.RandomState(1)
    return {s: {d: float(v) for d, v in zip(DATES, 100 * np.cumprod(1 + rng.normal(0, 0.01, len(DATES))))}
            for s in ('AAA', 'BBB', 'CCC')}


def _rows():
    return [{'symbol': 'AAA', 'quantity': 100, 'last_price': 100.0, 'verdict': 'SELL', 'trend_score': -50,
             'confidence': 70, 'stop_level': 90.0, 'exit_target': 160.0, 'rotation_sell_qty': 5,
             'rotation_target_symbol': 'X', 'indicators': {'rsi': 40}},
            {'symbol': 'BBB', 'quantity': 10, 'last_price': 100.0, 'verdict': 'HOLD', 'trend_score': 60,
             'indicators': {}},
            {'symbol': 'CCC', 'quantity': 10, 'last_price': 100.0, 'verdict': 'TRIM', 'trend_score': 0}]


def _run(rows):
    with patch.object(pa.db, 'write_config') as w:
        pa.apply_risk_targets(rows, _closes(), '2026-11-18')
    return w


def _strip(rows):
    out = copy.deepcopy(rows)
    for r in out:
        (r.get('indicators') or {}).pop('risk_size', None)
        if r.get('indicators') == {}:
            del r['indicators']
    return out


def test_risk_pass_leaves_verdict_and_rotation_identical():
    before, after = _rows(), _rows()
    _run(after)
    assert _strip(before) == _strip(after)


def test_risk_size_on_sell_grade_rows_only():
    rows = _rows()
    w = _run(rows)
    assert set(rows[0]['indicators']['risk_size']) == {'target_pct', 'current_pct', 'sell_qty_to_target'}
    assert 'risk_size' in rows[2]['indicators']
    assert 'risk_size' not in rows[1]['indicators']
    key, val = w.call_args[0]
    assert key == 'risk_targets_latest' and json.loads(val)['asof'] == '2026-11-18'


def test_risk_pass_failure_is_non_fatal():
    rows = _rows()
    with patch('risk_targets.build', side_effect=RuntimeError('boom')):
        _run(rows)
    assert _strip(rows) == _strip(_rows())
    with patch.object(pa.db, 'write_config', side_effect=RuntimeError('db')):
        pa.apply_risk_targets(_rows(), _closes(), '2026-11-18')


def test_shadow_sell_qty_uses_included_value_not_whole_book():
    import math
    rows = _rows() + [{'symbol': 'DDD', 'quantity': 1000, 'last_price': 100.0, 'verdict': 'HOLD'}]
    _run(rows)                              # DDD has no closes -> excluded from the risk read
    rs = rows[0]['indicators']['risk_size']
    included = 100 * 100.0 + 10 * 100.0 + 10 * 100.0   # AAA+BBB+CCC, the pct base
    want = math.floor(0.5 * (rs['current_pct'] - rs['target_pct']) / 100 * included / 100.0)
    assert 0 < want < 100
    assert rs['sell_qty_to_target'] == want
