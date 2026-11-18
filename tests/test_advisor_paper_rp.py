"""PICKING_RP paper book [P-68 phase 2]: monthly rebalance to the stored capped-HRP
targets, costs per side, Nifty 500 baseline. Real db layer on FakeSupabase (conftest)."""
import json
from unittest.mock import MagicMock, patch

import advisor_paper as ap
import costs
import database as db

COST = costs.round_trip_cost_pct() / 2 / 100


def _md(prices):
    md = MagicMock()
    md._instrument_cache = {}
    md.get_candles.side_effect = lambda key, i, d: (
        [{'timestamp': '2026-11-01', 'close': prices.get(key.replace('NSE:', ''), 20000)}])
    return md


def _rows():
    return [{'id': 'a', 'symbol': 'AAA', 'verdict': 'HOLD', 'quantity': 100, 'last_price': 100.0,
             'avg_price': 90.0},
            {'id': 'b', 'symbol': 'GOLDBEES', 'verdict': 'HOLD', 'quantity': 200, 'last_price': 50.0,
             'avg_price': 40.0}]       # book value 20,000


def _targets(asof, aaa=60.0, bbb=40.0, step=0.5):
    tg = [{'symbol': 'AAA', 'target_pct': aaa}]
    if bbb is not None:
        tg.append({'symbol': 'GOLDBEES', 'target_pct': bbb})
    db.write_config('risk_targets_latest', json.dumps({'asof': asof, 'step': step, 'targets': tg}))


def _bars(**closes):
    for d, c in closes.items():
        db.supabase.table('index_bars').insert(
            {'index_name': 'Nifty 500', 'trade_date': d.replace('_', '-'), 'close': c}).execute()


def _run(date, prices=None):
    with patch.object(db, 'get_official_advice_for_date', return_value=_rows()):
        return ap.run_paper_portfolio(_md(prices or {'AAA': 100.0, 'GOLDBEES': 50.0}), run_date=date)


def _rp():
    return db.paper_positions('PICKING_RP')


def _held(sym):
    return sum(int(p['qty']) for p in _rp() if p['is_open'] and p['symbol'] == sym)


def _equity(date):
    return {e['snapshot_date']: e for e in db.paper_equity_curve('PICKING_RP')}[date]['total_equity']


def test_rp_seeds_once_with_book_value():
    _bars(**{'2026_11_17': 1000.0})
    _targets('2026-11-18')
    _run('2026-11-18')
    seed = json.loads(db.get_config('advisor_paper_seed_rp'))
    assert seed['capital'] == 20000.0 and seed['seed_date'] == '2026-11-18'
    assert seed['nifty500_inception'] == 1000.0
    _run('2026-11-19')
    assert json.loads(db.get_config('advisor_paper_seed_rp')) == seed


def test_rp_rebalances_first_run_of_month_only():
    _bars(**{'2026_11_17': 1000.0})
    _targets('2026-11-18')
    _run('2026-11-18')
    n = len(_rp())
    assert n == 2 and all(p['is_open'] for p in _rp())
    _targets('2026-11-19', 50, 50)
    _run('2026-11-19')                      # same month -> no trade
    assert len(_rp()) == n and all(p['is_open'] for p in _rp())
    _targets('2026-12-01', 50, 50)
    _run('2026-12-01')                      # new month -> trade step of the gap (both buys here)
    assert sum(1 for p in _rp() if not p['is_open']) == 0
    assert sum(1 for p in _rp() if p['entry_date'] == '2026-12-01') == 2
    n = len(_rp())
    _targets('2026-12-02', 10, 90)
    _run('2026-12-02')                      # same month again -> no trade
    assert len(_rp()) == n


def test_rp_same_date_rerun_is_noop():
    _bars(**{'2026_11_17': 1000.0})
    _targets('2026-11-18')
    _run('2026-11-18')
    before = [dict(p) for p in _rp()]
    _run('2026-11-18')
    assert [dict(p) for p in _rp()] == before
    assert len(db.paper_equity_curve('PICKING_RP')) == 1


def test_rp_skips_when_targets_stale_but_snapshots():
    _bars(**{'2026_11_17': 1000.0})
    _targets('2026-11-17')                  # asof != run_date
    _run('2026-11-18')
    assert _rp() == []
    assert db.paper_equity_curve('PICKING_RP')[0]['total_equity'] == 20000.0


def test_rp_pays_cost_each_side():
    _bars(**{'2026_11_17': 1000.0})
    _targets('2026-11-18')
    _run('2026-11-18')
    eq = db.paper_equity_curve('PICKING_RP')[0]
    # bought ~100% of 20k; each rupee bought paid COST on entry
    spent = sum(p['qty'] * p['entry_price'] for p in _rp())
    assert all(abs(p['entry_price'] - px * (1 + COST)) < 1e-6
               for p, px in ((p, 100.0 if p['symbol'] == 'AAA' else 50.0) for p in _rp()))
    assert abs(eq['cash'] - (20000 - spent)) < 0.02 and eq['cash'] >= 0
    # selling at an unchanged price loses the sell-side cost too
    _targets('2026-12-01', 10, 90)          # AAA over target -> partial sell
    _run('2026-12-01')
    closed = [p for p in _rp() if not p['is_open']]
    assert closed
    assert all(abs(p['exit_price'] - px * (1 - COST)) < 1e-6
               for p, px in ((p, 100.0 if p['symbol'] == 'AAA' else 50.0) for p in closed))


def test_rp_baseline_nifty500():
    _bars(**{'2026_11_17': 1000.0, '2026_11_18': 1100.0, '2026_11_19': 1200.0})
    _targets('2026-11-18')
    _run('2026-11-18')                      # inception = latest <= run_date = 1100
    _run('2026-11-20')                      # lagging table -> latest <= run_date = 1200
    seed = json.loads(db.get_config('advisor_paper_seed_rp'))
    assert seed['nifty500_inception'] == 1100.0
    eq = {e['snapshot_date']: e for e in db.paper_equity_curve('PICKING_RP')}
    assert eq['2026-11-20']['baseline_equity'] == round(20000 * 1200 / 1100, 2)


def test_rp_trades_only_step_of_gap():
    _bars(**{'2026_11_17': 1000.0})
    _targets('2026-11-18')                  # seed: C=0, step 0.5 of T
    _run('2026-11-18')
    assert _held('AAA') == 60 and _held('GOLDBEES') == 80      # T=120 / 160
    e0 = _equity('2026-11-18')
    _targets('2026-12-01', 30, 70, step=0.25)
    _run('2026-12-01')
    t_a, t_g = int(0.30 * e0 / 100.0), int(0.70 * e0 / 50.0)
    assert _held('AAA') == 60 + int(0.25 * (t_a - 60))
    assert _held('GOLDBEES') == 80 + int(0.25 * (t_g - 80))
    assert db.paper_equity_curve('PICKING_RP')[-1]['cash'] >= 0


def test_rp_cost_charged_on_traded_value_only():
    _bars(**{'2026_11_17': 1000.0})
    _targets('2026-11-18')
    _run('2026-11-18')
    e0 = _equity('2026-11-18')
    lots = {p['id']: dict(p) for p in _rp()}
    _targets('2026-12-01', 30, 70)
    _run('2026-12-01')
    traded = abs(_held('AAA') - 60) * 100.0 + abs(_held('GOLDBEES') - 80) * 50.0
    assert traded > 0
    # prices unchanged: equity drops by exactly the cost on the traded rupees
    assert abs((e0 - _equity('2026-12-01')) - COST * traded) < 0.05
    for p in _rp():                         # untraded seed lots keep their fill price
        if p['id'] in lots:
            assert p['entry_price'] == lots[p['id']]['entry_price']


def test_rp_name_dropped_from_targets_shrinks_by_step():
    _bars(**{'2026_11_17': 1000.0})
    _targets('2026-11-18')
    _run('2026-11-18')
    assert _held('GOLDBEES') == 80
    _targets('2026-12-01', 100, None)       # GOLDBEES gone -> T=0
    _run('2026-12-01')
    assert _held('GOLDBEES') == 40
    sold = [p for p in _rp() if p['symbol'] == 'GOLDBEES' and not p['is_open']]
    assert sum(p['qty'] for p in sold) == 40 and sold[0]['exit_reason'] == 'REBALANCE'
    assert abs(sold[0]['realized_pnl'] - (50 * (1 - COST) - 50 * (1 + COST)) * 40) < 0.02


def test_rp_seed_failure_never_blocks_other_books():
    _bars(**{'2026_11_17': 1000.0})
    with patch.object(ap, '_seed_rp', side_effect=RuntimeError('boom')):
        assert _run('2026-11-18')
    assert len(db.paper_equity_curve('MANAGEMENT')) == 1
    assert len(db.paper_equity_curve('PICKING')) == 1


def test_rp_guard_not_written_when_insert_fails():
    """11-18 prod: the book CHECK rejected PICKING_RP rows (insert swallowed,
    returned 0) yet the month guard was written, so no retry until December."""
    _bars(**{'2026_11_17': 1000.0})
    _targets('2026-11-18')
    with patch.object(db, 'insert_paper_positions', return_value=0):
        _run('2026-11-18')
    assert db.get_config('advisor_paper_rp_last_rebalance') is None


def test_fake_rejects_unknown_book():
    import pytest
    with pytest.raises(ValueError):
        db.supabase.table('advisor_paper_positions').insert(
            {'book': 'NOPE', 'symbol': 'X', 'source': 'T', 'qty': 1,
             'entry_price': 1.0, 'entry_date': '2026-11-19', 'is_open': True}).execute()
    with pytest.raises(ValueError):
        db.supabase.table('advisor_paper_equity').upsert(
            {'book': 'NOPE', 'snapshot_date': '2026-11-19'},
            on_conflict='book,snapshot_date').execute()



def test_rp_failed_insert_leaves_book_untouched_and_retry_steps_once():
    """A rejected insert must not leave sells applied: the retry would step the
    gap a second time (over-sell) and the partly-sold lot's closed row is lost."""
    _bars(**{'2026_11_17': 1000.0})
    _targets('2026-11-18')
    _run('2026-11-18')
    before = sorted((p['symbol'], p['qty'], p['is_open']) for p in _rp())
    _targets('2026-12-01', 100, None)       # GOLDBEES gone -> partial sell + AAA buy
    with patch.object(db, 'insert_paper_positions', return_value=0):
        _run('2026-12-01')
    assert sorted((p['symbol'], p['qty'], p['is_open']) for p in _rp()) == before
    _run('2026-12-01')                      # clean retry
    assert _held('GOLDBEES') == 40
