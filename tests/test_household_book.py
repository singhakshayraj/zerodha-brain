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
    assert out['since'] == 2.01 and out['1m'] == 2.01 and out['3m'] == 2.01


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
    out = xray(ps, {'FOO': ''}, {}, {})
    rows = {r['name']: r for r in out['sectors']}
    assert rows['ETFs (gold, silver, index)']['pct'] == 50.0
    assert not rows['ETFs (gold, silver, index)']['breach']
    assert not rows['Outside Nifty 500']['breach']
    assert out['etf_pct'] == 50.0 and out['etf_breach'] is True


def test_overlap_name_counted_once_and_flagged():
    ps = [_pos('A', 'TCS', 2, 100), _pos('B', 'TCS', 2, 100), _pos('A', 'INFY', 96, 100)]
    names = {n['symbol']: n for n in xray(ps, {'TCS': 'IT', 'INFY': 'IT'}, {}, {})['names']}
    assert names['TCS']['pct'] == 4.0 and names['TCS']['accounts'] == ['A', 'B']
    assert names['TCS']['breach'] == 'overlap'
    assert names['INFY']['breach'] == 'name'
    out = xray(ps, {'TCS': 'IT', 'INFY': 'IT'}, {}, {})
    assert out['sectors'][0]['pct'] == 100.0 and out['sectors'][0]['breach'] is True


def test_size_drift_vs_index_mix():
    ps = [_pos('A', 'L', 50, 100), _pos('A', 'S', 30, 100), _pos('A', 'M', 20, 100)]
    out = xray(ps, {}, {'L': 'large', 'S': 'small', 'M': 'mid'}, {},
               index_size_mix={'large': 70.0, 'mid': 20.0, 'small': 10.0})
    z = {r['size']: r for r in out['sizes']}
    assert z['large']['drift_pp'] == -20.0 and z['large']['breach'] is True
    assert z['small']['drift_pp'] == 20.0 and z['small']['breach'] is True
    assert z['mid']['drift_pp'] == 0.0 and z['mid']['breach'] is False
    assert DEFAULT_TARGETS['source'] == 'default' and DEFAULT_TARGETS['size_tolerance_pp'] == 10.0


def _walk(seed, n=61):
    import numpy as np
    r = np.random.RandomState(seed).normal(0, 0.01, n - 1)
    return list(100 * np.cumprod(1 + np.r_[0, r]))


def test_risk_effective_bets_two_identical_names_is_one():
    a = _walk(1)
    ps = [_pos('A', 'X', 10, 100), _pos('A', 'Y', 10, 100)]
    out = risk(ps, {'X': a, 'Y': list(a)})
    assert abs(out['effective_bets'] - 1.0) < 0.05 and out['n_used'] == 2
    ps.append(_pos('A', 'Z', 10, 100))
    out3 = risk(ps, {'X': a, 'Y': list(a), 'Z': _walk(2)})
    assert out3['effective_bets'] > 1.3 and len(out3['top_contributors']) == 3
    short = risk(ps, {'X': a, 'Y': list(a), 'Z': a[:10]})
    assert short['excluded'] == ['Z'] and short['n_used'] == 2
    assert risk(ps[:1], {'X': a})['effective_bets'] is None


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
