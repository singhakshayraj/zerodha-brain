from household_book import twr, benchmark_twr, xirr, max_drawdown, net_worth


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
