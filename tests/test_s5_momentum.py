"""S5 trend-filtered momentum paper book [P-71] — rules: zerodha-trading
docs/reference/s1_momentum/PREREG_S5.md (trial 265)."""
from datetime import date, timedelta
from unittest.mock import patch

import pytest

import s5_momentum as s5

ASOF = '2026-12-31'


def _days(n, end=date(2026, 12, 30)):
    out, d = [], end
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d -= timedelta(days=1)
    return out[::-1]


def _series(n, growth, start=100.0, volume=1e6, end=date(2026, 12, 30)):
    """Geometric drift with the same ±1% zig-zag on every name, so σ is equal
    and momentum order = growth order."""
    bars, px = [], start
    for i, d in enumerate(_days(n, end)):
        px *= (1 + growth) * (1.01 if i % 2 else 0.99)
        bars.append({'timestamp': d, 'close': px, 'volume': volume})
    return bars


def _nifty(rising=True, n=260):
    bars = _series(n, 0.001 if rising else 0.0)
    if not rising:                              # crash the tail below the 200-SMA
        for b in bars[-20:]:
            b['close'] *= 0.80
    return bars


def test_rank_orders_by_momentum():
    bars = {'AAA': _series(300, 0.003), 'BBB': _series(300, 0.001), 'CCC': _series(300, 0.0)}
    out = s5.rank(bars, _nifty(), ASOF)
    assert out['ranked'][:3] == ['AAA', 'BBB', 'CCC']
    assert out['eligible'] == 3


def test_rank_excludes_short_history_and_penny():
    bars = {'AAA': _series(300, 0.003), 'BBB': _series(300, 0.001),
            'SHORT': _series(259, 0.005), 'PENNY': _series(300, 0.002, start=10.0)}
    assert bars['PENNY'][-1]['close'] < 20
    out = s5.rank(bars, _nifty(), ASOF)
    assert 'SHORT' not in out['ranked'] and 'PENNY' not in out['ranked']


def test_rank_liquidity_cut():
    bars = {'AAA': _series(300, 0.003), 'BBB': _series(300, 0.001),
            'THIN': _series(300, 0.004, volume=10.0)}
    with patch.object(s5, 'TOP_LIQ', 2):
        out = s5.rank(bars, _nifty(), ASOF)
    assert 'THIN' not in out['ranked'] and len(out['ranked']) == 2


def test_risk_off_below_sma200():
    bars = {'AAA': _series(300, 0.003), 'BBB': _series(300, 0.001)}
    assert s5.rank(bars, _nifty(rising=False), ASOF)['risk_off'] is True
    assert s5.rank(bars, _nifty(rising=True), ASOF)['risk_off'] is False


def test_rank_ignores_asof_bar():
    bars = {'AAA': _series(300, 0.003), 'BBB': _series(300, 0.001), 'CCC': _series(300, 0.0)}
    bars['CCC'].append({'timestamp': ASOF, 'close': 1e6, 'volume': 1e9})
    assert s5.rank(bars, _nifty(), ASOF)['ranked'][:3] == ['AAA', 'BBB', 'CCC']


def test_rank_raises_without_nifty_history():
    bars = {'AAA': _series(300, 0.003), 'BBB': _series(300, 0.001)}
    with pytest.raises(ValueError):
        s5.rank(bars, _nifty(n=150), ASOF)


def test_no_order_imports():
    src = open(s5.__file__).read()
    assert 'order_manager' not in src and 'place_order' not in src
