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


# ── Task 4: rebalance / split patch / snapshot / run_s5 ─────────────────────
import json
from unittest.mock import MagicMock

import advisor_paper as ap
import config
import database as db

UNIV = [f'S{i:02d}' for i in range(1, 31)]


def _md(no_bar=(), nifty_rising=True, end=date(2027, 1, 29)):
    """Day bars ending at `end` (growth falls with the symbol number, so S01 is
    the strongest) and one 15:20 5-min bar per asked day at the last close."""
    day = {s: _series(330, 0.003 - 0.0001 * i, start=100.0, end=end) for i, s in enumerate(UNIV)}
    nifty = _nifty(rising=nifty_rising, n=330)
    md = MagicMock()
    md._instrument_cache = {}

    def candles(key, interval, days):
        sym = key.replace('NSE:', '')
        if interval == 'day':
            return nifty if sym == 'NIFTY 50' else day.get(sym, [])
        if sym in no_bar:
            return []
        last = (day.get(sym) or nifty)[-1]['close']
        return [{'timestamp': f'{d} 15:20:00+05:30', 'close': last}
                for d in ('2026-12-31', '2027-01-29')]
    md.get_candles.side_effect = candles
    return md


@pytest.fixture(autouse=True)
def _universe(monkeypatch):
    monkeypatch.setattr(config, 'NIFTY500_INSTRUMENT_TOKENS',
                        {f'NSE:{s}': i + 1 for i, s in enumerate(UNIV)})
    monkeypatch.setattr(config, 'ADVISOR_UNIVERSE_SCAN_DELAY_MS', 0)
    monkeypatch.setattr(config, 'S5_PAPER_ENABLED', True)


def _open():
    return [p for p in db.paper_positions('S5') if p['is_open']]


def _last():
    raw = db.get_config('s5_last_rebalance')
    return json.loads(raw) if raw else None


def test_fills_match_kit_costs():
    assert s5.buy_fill(1000.0, 5) == pytest.approx(5010.93703 / 5, abs=1e-6)
    assert s5.sell_fill(1000.0, 5) == pytest.approx(4974.47297 / 5, abs=1e-6)


def test_pending_rebalance():
    assert s5.pending_rebalance('2026-12-31', None) == ('2026-12-31', 'run')
    assert s5.pending_rebalance('2026-12-30', None) is None
    assert s5.pending_rebalance('2027-01-08', None) == ('2026-12-31', 'skip')
    assert s5.pending_rebalance('2027-01-07', {'date': '2026-12-31', 'status': 'run'}) is None


def test_first_rebalance_buys_ten_and_seeds():
    out = s5.run_s5(_md(), today='2026-12-31')
    assert out['rebalanced'] == '2026-12-31'
    assert sorted(p['symbol'] for p in _open()) == UNIV[:10]
    assert json.loads(db.get_config('s5_seed'))['capital'] == 50000.0
    assert ap._reconstruct_cash('S5', db.paper_positions('S5'), 50000.0) >= 0
    assert _last()['status'] == 'run'


def test_risk_off_sells_all_buys_none():
    s5.run_s5(_md(), today='2026-12-31')
    s5.run_s5(_md(nifty_rising=False), today='2027-01-29')
    assert _open() == [] and _last() == {**_last(), 'date': '2027-01-29', 'status': 'run'}


def test_buffer_keeps_rank_20_sells_rank_21():
    s5.run_s5(_md(), today='2026-12-31')
    order = UNIV[10:28] + ['S03', 'S01', 'S02'] + UNIV[3:10] + UNIV[28:]
    assert order.index('S01') + 1 == 20 and order.index('S02') + 1 == 21
    with patch.object(s5, 'rank', return_value={'ranked': order, 'risk_off': False, 'eligible': 30}):
        s5.run_s5(_md(), today='2027-01-29')
    held = {p['symbol'] for p in _open()}
    assert 'S01' in held and 'S02' not in held and len(held) == 10


def test_missing_1520_bar():
    s5.run_s5(_md(), today='2026-12-31')
    order = UNIV[10:] + UNIV[:10]                    # every held name drops out
    md = _md(no_bar=('S01', 'S11'))
    with patch.object(s5, 'rank', return_value={'ranked': order, 'risk_off': False, 'eligible': 30}):
        s5.run_s5(md, today='2027-01-29')
    s01 = [p for p in db.paper_positions('S5') if p['symbol'] == 'S01'][0]
    assert s01['is_open'] is False and s01['exit_date'] == '2027-01-29'   # sold at last close
    held = sorted(p['symbol'] for p in _open())
    assert 'S11' not in held and held == UNIV[11:21]


def test_split_patch():
    db.insert_paper_positions([ap._open_position('S5', 'S01', 'S5', {}, 5, 1000.0, '2026-12-31')])
    pos = _open()
    bars = {'S01': [{'timestamp': '2026-12-31', 'close': 500.0}]}
    assert s5.patch_splits(pos, bars) == 1
    p = _open()[0]
    assert p['qty'] == 10 and float(p['entry_price']) == pytest.approx(500.0)
    assert s5.patch_splits(_open(), bars) == 0


def test_rerun_same_day_no_duplicates():
    s5.run_s5(_md(), today='2026-12-31')
    n = len(db.paper_positions('S5'))
    s5.run_s5(_md(), today='2026-12-31')
    assert len(db.paper_positions('S5')) == n == 10


def test_insert_failure_no_guard():
    with patch.object(db, 'insert_paper_positions', return_value=0):
        s5.run_s5(_md(), today='2026-12-31')
    assert _last() is None


def test_late_beyond_catchup_skips():
    out = s5.run_s5(_md(), today='2027-01-08')
    assert out['rebalanced'] is None and _open() == []
    assert _last() == {'date': '2026-12-31', 'status': 'skipped'}


def test_no_nifty_1520_bar_aborts():
    """A month-end that was really a holiday (calendar gap) has no Nifty bar:
    no trades, no guard — retried, then skipped after the catch-up window."""
    s5.run_s5(_md(no_bar=('NIFTY 50',)), today='2026-12-31')
    assert _open() == [] and _last() is None


def test_snapshot_baseline():
    for d, c in (('2026-12-30', 1000.0), ('2027-01-04', 1100.0)):
        db.supabase.table('index_bars').insert(
            {'index_name': 'Nifty200 Momentum 30', 'trade_date': d, 'close': c}).execute()
    s5.run_s5(_md(), today='2026-12-31')
    out = s5.run_s5(_md(), today='2027-01-05')
    assert out == {'rebalanced': None, 'snapshot': True}
    eq = {e['snapshot_date']: e for e in db.paper_equity_curve('S5')}
    assert eq['2027-01-05']['baseline_equity'] == pytest.approx(55000.0)
    assert eq['2027-01-05']['open_positions'] == 10
