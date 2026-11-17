from household_book import (twr, benchmark_twr, xirr, max_drawdown, net_worth,
                            xray, risk, tax_summary, DEFAULT_TARGETS)


def _row(d, pnl, vo, nifty=0.0, buys=0.0):
    return {'date': d, 'pnl': pnl, 'value_open': vo, 'value_close': vo + (pnl or 0) + buys,
            'nifty_pnl': nifty, 'buys_inr': buys, 'sells_inr': 0.0}


def test_twr_ignores_deposits():
    daily = [_row('2026-01-01', 1000, 100000),
             _row('2026-01-02', 0, 101000, buys=100000),
             _row('2026-01-03', 0, 201000)]
    out = twr(daily)
    assert out['start'] == '2026-01-01'
    assert [p['index'] for p in out['series']] == [101.0, 101.0, 101.0]
    assert out['since'] == 1.0


def test_twr_chains_returns():
    daily = [_row('2026-01-01', 1000, 100000), _row('2026-01-02', 1010, 101000)]
    out = twr(daily)
    assert [p['index'] for p in out['series']] == [101.0, 102.01]
    assert out['since'] == 2.01 and out['1m'] is None


def test_twr_skips_bad_rows_and_sorts():
    daily = [_row('2026-01-02', 1010, 101000), _row('2026-01-01', 1000, 100000),
             _row('2026-01-03', None, 100000), _row('2026-01-04', 5, 0)]
    assert len(twr(daily)['series']) == 2


def test_benchmark_from_nifty_pnl():
    daily = [_row('2026-01-01', 5000, 100000, nifty=2000), _row('2026-01-02', 0, 100000, nifty=2000)]
    out = benchmark_twr(daily)
    assert [p['index'] for p in out['series']] == [102.0, 104.04]


def test_xirr_known_value():
    r = xirr([('2025-01-01', -10000), ('2026-01-01', 11000)])
    assert abs(r - 10.0) < 0.05


def test_xirr_no_sign_change_is_none():
    assert xirr([('2025-01-01', -100), ('2026-01-01', -50)]) is None


def test_max_drawdown():
    s = [{'date': 'a', 'index': 100}, {'date': 'b', 'index': 120},
         {'date': 'c', 'index': 90}, {'date': 'd', 'index': 110}]
    assert max_drawdown(s) == {'pct': -25.0, 'peak': 'b', 'trough': 'c'}
    assert max_drawdown([]) == {'pct': 0.0, 'peak': None, 'trough': None}


def test_net_worth_by_account_and_class():
    pos = [{'account': 'A', 'symbol': 'X', 'quantity': 10, 'last_price': 100.5, 'asset_class': 'eq'},
           {'account': 'B', 'symbol': 'Y', 'quantity': 2, 'last_price': 50, 'asset_class': 'mf'},
           {'account': 'B', 'symbol': 'Z', 'quantity': 3, 'last_price': None, 'asset_class': 'eq'}]
    out = net_worth(pos)
    assert out['total'] == 1105.0
    assert out['by_account'] == {'A': 1005.0, 'B': 100.0}
    assert out['by_asset_class'] == {'eq': 1005.0, 'mf': 100.0}
    assert out['unpriced'] == ['Z']


def _pos(acct, sym, q, px, cls='equity', avg=None):
    return {'account': acct, 'symbol': sym, 'quantity': q, 'avg_price': avg or px,
            'last_price': px, 'asset_class': cls}


def test_not_sectors_never_breach():
    ps = [_pos('A', 'GOLDBEES', 50, 100, 'etf_gold_silver'), _pos('A', 'FOO', 50, 100)]
    out = xray(ps, {'FOO': ''}, {}, {}, {})
    rows = {r['name']: r for r in out['sectors']}
    assert rows['ETFs (gold, silver, index)']['pct'] == 50.0
    assert not rows['ETFs (gold, silver, index)']['breach']
    assert not rows['Outside Nifty 500']['breach']
    assert out['etf_pct'] == 50.0 and out['etf_breach'] is True


def test_overlap_name_counted_once_and_flagged():
    ps = [_pos('A', 'TCS', 2, 100), _pos('B', 'TCS', 2, 100), _pos('A', 'INFY', 96, 100)]
    names = {n['symbol']: n for n in xray(ps, {'TCS': 'IT', 'INFY': 'IT'}, {}, {}, {})['names']}
    assert names['TCS']['pct'] == 4.0 and names['TCS']['accounts'] == ['A', 'B']
    assert names['TCS']['breach'] == 'overlap'
    assert names['INFY']['breach'] == 'name'
    out = xray(ps, {'TCS': 'IT', 'INFY': 'IT'}, {}, {}, {})
    assert out['sectors'][0]['pct'] == 100.0 and out['sectors'][0]['breach'] is True


def test_size_drift_vs_index_mix():
    ps = [_pos('A', 'L', 50, 100), _pos('A', 'S', 30, 100), _pos('A', 'M', 20, 100)]
    mix = {'large': 70.0, 'mid': 20.0, 'small': 10.0}
    out = xray(ps, {}, {'L': 'large', 'S': 'small', 'M': 'mid'}, {}, mix)
    assert xray(ps, {}, {'L': 'large', 'S': 'small', 'M': 'mid'}, {}, mix, DEFAULT_TARGETS) == out
    z = {r['size']: r for r in out['sizes']}
    assert z['large']['drift_pp'] == -20.0 and z['large']['breach'] is True
    assert z['small']['drift_pp'] == 20.0 and z['small']['breach'] is True
    assert z['mid']['drift_pp'] == 0.0 and z['mid']['breach'] is False
    assert DEFAULT_TARGETS['source'] == 'default' and DEFAULT_TARGETS['size_tolerance_pp'] == 10.0


def _walk(seed, n=61):
    import numpy as np
    r = np.random.RandomState(seed).normal(0, 0.01, n - 1)
    return list(100 * np.cumprod(1 + np.r_[0, r]))


def _dated(vals):
    from datetime import date, timedelta
    d0 = date(2026, 1, 1)
    return {(d0 + timedelta(days=i)).isoformat(): v for i, v in enumerate(vals)}


def test_risk_effective_bets_two_identical_names_is_one():
    a = _dated(_walk(1))
    ps = [_pos('A', 'X', 10, 100), _pos('A', 'Y', 10, 100)]
    out = risk(ps, {'X': a, 'Y': dict(a)})
    assert abs(out['effective_bets'] - 1.0) < 0.05 and out['n_used'] == 2
    ps.append(_pos('A', 'Z', 10, 100))
    out3 = risk(ps, {'X': a, 'Y': dict(a), 'Z': _dated(_walk(2))})
    assert out3['effective_bets'] > 1.3 and len(out3['top_contributors']) == 3
    short = risk(ps, {'X': a, 'Y': dict(a), 'Z': _dated(_walk(2, 10))})
    assert short['excluded'] == ['Z'] and short['n_used'] == 2
    assert risk(ps[:1], {'X': a})['effective_bets'] is None


def test_risk_aligns_on_common_dates_when_a_day_is_missing():
    a = _dated(_walk(1))
    b = dict(a)
    del b[sorted(b)[30]]
    out = risk([_pos('A', 'X', 10, 100), _pos('A', 'Y', 10, 100)], {'X': a, 'Y': b})
    assert abs(out['effective_bets'] - 1.0) < 0.05 and out['n_used'] == 2


def test_risk_only_equity_and_etf():
    a = _dated(_walk(1))
    ps = [_pos('A', 'X', 10, 100), _pos('A', 'Y', 10, 100, 'etf_gold_silver'),
          _pos('A', 'F', 10, 100, 'fund_unconfirmed'), _pos('A', 'O', 10, 100, 'other')]
    out = risk(ps, {'X': a, 'Y': dict(a), 'F': dict(a), 'O': dict(a)})
    assert out['n_used'] == 2 and {r['symbol'] for r in out['top_contributors']} == {'X', 'Y'}


def test_tax_summary_splits_lots_and_lists_crossings_90d():
    from datetime import date
    ps = [_pos('A', 'AAA', 10, 120, avg=100), _pos('A', 'BBB', 10, 80, avg=100)]
    lots = {'A': {'AAA': [{'qty': 4, 'price': 100, 'date': '2025-01-01'},
                          {'qty': 6, 'price': 100, 'date': '2025-11-20'}],
                  'BBB': [{'qty': 10, 'price': 100, 'date': '2026-10-01'}]}}
    fy = {'A': {'ytd_net_realised_inr': 500.0, 'fy_known': True}, 'B': {'fy_known': False}}
    out = tax_summary(ps, lots, fy, date(2026, 11, 17))
    assert out['unrealised_ltcg'] == 80.0 and out['unrealised_stcg'] == 120.0 - 200.0
    assert out['by_account']['A'] == {'stcg': -80.0, 'ltcg': 80.0}
    assert out['ltcg_calendar'] == [{'symbol': 'AAA', 'account': 'A', 'date': '2026-11-21', 'qty': 6}]
    assert out['harvest'] == [{'symbol': 'BBB', 'account': 'A', 'loss_inr': 200.0}]
    assert out['fy_realised'] == {'A': 500.0, 'B': None}


# ---- rebalance -------------------------------------------------------------
from datetime import date as _date
from household_book import rebalance, _h, _val


def _xr(ps, sectors=None, iw=None):
    return xray(ps, sectors or {}, {}, iw or {}, {}, DEFAULT_TARGETS)


def _fill(n, v):   # n equal 5% -> small names, no breach
    return [_pos('A', f'F{i}', 1, v) for i in range(n)]


def test_trim_brings_breach_inside_target_whole_shares():
    ps = [_pos('A', 'BIG', 112, 100)] + _fill(20, 4440)          # total 100000, BIG 11.2%
    out = rebalance(_xr(ps), ps, {}, {}, DEFAULT_TARGETS, _date(2026, 11, 17))
    trims = [r for r in out if r['kind'] == 'trim']
    assert len(trims) == 1
    t = trims[0]
    assert t['symbol'] == 'BIG' and isinstance(t['qty'], int) and t['qty'] == 32
    assert (11200 - t['freed_inr']) / 100000 * 100 <= 8.0
    assert t['freed_inr'] == 3200 and t['cost_inr'] > 0 and t['reason'] == 'BIG 11.2% > 8% name cap'


def test_trim_never_exceeds_held():
    ps = [_pos('A', 'BIG', 5, 1000), _pos('B', 'BIG', 3, 1000)] + _fill(20, 100)
    out = [r for r in rebalance(_xr(ps), ps, {}, {}, dict(DEFAULT_TARGETS, name_cap_pct=1.0),
                                _date(2026, 11, 17)) if r['kind'] == 'trim' and r['symbol'] == 'BIG']
    held = {('A', 'BIG'): 5, ('B', 'BIG'): 3}
    for t in out:
        held[(t['account'], t['symbol'])] -= t['qty']
    assert all(v >= 0 for v in held.values())
    assert sum(t['qty'] for t in out) == 8          # cap unreachable -> sells exactly what exists


def test_trim_prefers_lowest_tax_lots():
    ps = [_pos('A', 'BIG', 20, 100), _pos('B', 'BIG', 20, 100)] + _fill(20, 100)
    ps = [p if p['symbol'] != 'BIG' else dict(p, last_price=150) for p in ps]   # BIG ~ 6000/9000
    ps += [_pos('A', f'G{i}', 1, 5000) for i in range(2)]
    lots = {'A': {'BIG': [{'qty': 20, 'price': 50, 'date': '2026-10-01'}]},     # short-term gain
            'B': {'BIG': [{'qty': 20, 'price': 50, 'date': '2024-01-01'}]}}     # long-term, under exemption
    fy = {'A': {'fy_known': True}, 'B': {'fy_known': True}}
    out = [r for r in rebalance(_xr(ps), ps, lots, fy, DEFAULT_TARGETS, _date(2026, 11, 17))
           if r['kind'] == 'trim' and r['symbol'] == 'BIG']
    assert out[0]['account'] == 'B' and out[0]['tax_inr'] == 0


def test_underweight_sector_names_only():
    ps = [_pos('A', 'X', 10, 100)]
    xr = {'names': [], 'sectors': [{'name': 'IT', 'pct': 0, 'drift_pp': -9.0, 'breach': False},
                                   {'name': 'Banks', 'pct': 0, 'drift_pp': -3.0, 'breach': False},
                                   {'name': 'Auto', 'pct': 5, 'drift_pp': 2.0, 'breach': False}]}
    assert rebalance(xr, ps, {}, {}, DEFAULT_TARGETS, _date(2026, 11, 17)) == [
        {'kind': 'underweight', 'sector': 'IT', 'gap_pp': 9.0},
        {'kind': 'underweight', 'sector': 'Banks', 'gap_pp': 3.0}]


# ---- fix wave --------------------------------------------------------------
def test_unheld_index_sector_listed_as_underweight():
    ps = [_pos('A', 'X', 10, 100)]
    xr = xray(ps, {'X': 'IT'}, {}, {'IT': 20.0, 'Banks': 15.0, 'ETFs (gold, silver, index)': 4.0}, {})
    b = [s for s in xr['sectors'] if s['name'] == 'Banks'][0]
    assert b == {'name': 'Banks', 'pct': 0.0, 'index_pct': 15.0, 'target_pct': 20.0,
                 'drift_pp': -15.0, 'breach': False}
    assert not any(s['name'] == 'ETFs (gold, silver, index)' for s in xr['sectors'])
    out = rebalance(xr, ps, {}, {}, DEFAULT_TARGETS, _date(2026, 11, 17))
    assert [r for r in out if r['kind'] == 'underweight'][0] == {'kind': 'underweight', 'sector': 'Banks', 'gap_pp': 15.0}


def test_etf_does_not_change_size_mix():
    base = [_pos('A', 'L', 50, 100), _pos('A', 'S', 50, 100)]
    sz = {'L': 'large', 'S': 'small'}
    a = xray(base, {}, sz, {}, {'large': 50.0, 'small': 50.0})['sizes']
    b = xray(base + [_pos('A', 'GOLDBEES', 100, 100, 'etf_gold_silver')], {}, sz, {},
             {'large': 50.0, 'small': 50.0})['sizes']
    assert a == b and not any(r['size'] == 'micro' for r in b)


def test_dated_account_trimmed_first_undated_flagged_unknown():
    ps = [_pos('A', 'BIG', 20, 150), _pos('B', 'BIG', 20, 150)] + _fill(20, 100)
    ps += [_pos('A', f'G{i}', 1, 5000) for i in range(2)]
    lots = {'A': {'BIG': [{'qty': 20, 'price': 50, 'date': '2026-10-01'}]}}    # dated, STCG > 0; B undated
    fy = {'A': {'fy_known': True}, 'B': {'fy_known': True}}
    out = [r for r in rebalance(_xr(ps), ps, lots, fy, DEFAULT_TARGETS, _date(2026, 11, 17))
           if r['kind'] == 'trim' and r['symbol'] == 'BIG']
    assert out[0]['account'] == 'A' and out[0]['tax_inr'] > 0 and out[0]['tax_known'] is True
    assert [t for t in out if t['account'] == 'B'][0]['tax_known'] is False


def test_undated_account_not_chosen_over_dated_even_if_it_looks_free():
    ps = [_pos('A', 'BIG', 20, 150), _pos('B', 'BIG', 20, 150)] + _fill(20, 100)
    ps += [_pos('A', f'G{i}', 1, 5000) for i in range(2)]
    lots = {'B': {'BIG': [{'qty': 20, 'price': 50, 'date': '2026-10-01'}]}}    # A undated (tax 0 by bug)
    fy = {'A': {'fy_known': True}, 'B': {'fy_known': True}}
    out = [r for r in rebalance(_xr(ps), ps, lots, fy, DEFAULT_TARGETS, _date(2026, 11, 17))
           if r['kind'] == 'trim' and r['symbol'] == 'BIG']
    assert out[0]['account'] == 'B'


def test_both_account_name_above_name_cap_trimmed_to_overlap_cap():
    ps = [_pos('A', 'DUP', 5, 100), _pos('B', 'DUP', 5, 100)] + _fill(18, 100)   # DUP 1000 of 2800 -> fix below
    ps = [_pos('A', 'DUP', 5, 22.5), _pos('B', 'DUP', 5, 22.5)] + _fill(18, 100)   # 225 of 2025 ~ 11%
    xr = _xr(ps)
    d = [n for n in xr['names'] if n['symbol'] == 'DUP'][0]
    assert d['pct'] > 8.0 and d['breach'] == 'overlap'
    out = rebalance(xr, ps, {}, {}, DEFAULT_TARGETS, _date(2026, 11, 17))
    freed = sum(t['freed_inr'] for t in out if t['kind'] == 'trim' and t['symbol'] == 'DUP')
    tot = sum(_val(p) for p in ps)
    assert (d['pct'] / 100 * tot - freed) / tot * 100 <= 3.0


def test_max_drawdown_counts_drop_from_base_100():
    s = [{'date': 'a', 'index': 95.0}, {'date': 'b', 'index': 97.0}]
    assert max_drawdown(s, start='s') == {'pct': -5.0, 'peak': 's', 'trough': 'a'}


def test_short_window_returns_are_none():
    out = twr([_row('2026-01-01', 1000, 100000), _row('2026-01-02', 1010, 101000)])
    assert out['1m'] is None and out['3m'] is None and out['since'] == 2.01
