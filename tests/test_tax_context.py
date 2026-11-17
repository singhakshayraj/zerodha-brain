import json
from unittest.mock import patch

import tax_context as tc
from tests.fake_supabase import FakeSupabase


def _call(sym, verdict, source='advice'):
    return {'source': source, 'account': 'primary', 'run_date': '2026-10-01', 'symbol': sym, 'direction': -1,
            'attrs': {'verdict': verdict}}


def test_cost_curve_from_outcomes():
    calls = [_call('A', 'SELL'), _call('B', 'TRIM'), _call('C', 'HOLD'), _call('D', 'SELL', 'universe')]
    k = lambda c: (c['source'], c['account'], c['run_date'], c['symbol'])
    rows = {k(calls[0]): [{'td': 5, 'ret_pct': -2.0}, {'td': 20, 'ret_pct': 4.0}],
            k(calls[1]): [{'td': 5, 'ret_pct': -3.0}],
            k(calls[2]): [{'td': 5, 'ret_pct': 50.0}], k(calls[3]): [{'td': 5, 'ret_pct': 50.0}]}
    assert tc.cost_curve(rows, calls) == {'5': {'n': 2, 'mean_ret_pct': -2.5}, '20': {'n': 1, 'mean_ret_pct': 4.0}}


def _fake():
    f = FakeSupabase()
    f.tables['tradebook'] = [
        {'symbol': 'AAA', 'account': 'primary', 'trade_type': 'buy', 'quantity': 10, 'price': 100, 'trade_date': '2025-01-01', 'executed_at': 'a', 'trade_id': '1'},
        {'symbol': 'AAA', 'account': 'primary', 'trade_type': 'buy', 'quantity': 5, 'price': 120, 'trade_date': '2025-06-01', 'executed_at': 'b', 'trade_id': '2'},
        {'symbol': 'AAA', 'account': 'primary', 'trade_type': 'sell', 'quantity': 4, 'price': 130, 'trade_date': '2025-07-01', 'executed_at': 'c', 'trade_id': '3'},
        {'symbol': 'AAA', 'account': 'natwar', 'trade_type': 'buy', 'quantity': 99, 'price': 1, 'trade_date': '2025-01-01', 'executed_at': 'a', 'trade_id': '4'}]
    f.tables['pnl_statements'] = [{'account': 'primary', 'period_from': '2026-04-01', 'period_to': '2026-11-01',
                                   'summary': {'Realized P&L': -47479.0}},
                                  {'account': 'primary', 'period_from': '2025-04-01', 'summary': {'Realized P&L': 999}}]
    f.tables['candles'] = [{'symbol': 'AAA', 'interval': 'day', 'trade_date': f'2026-11-{d:02d}', 'close': 10.0, 'volume': 100}
                           for d in range(1, 11)] + [{'symbol': 'ZZZ', 'interval': 'day', 'trade_date': '2026-11-05', 'close': 1, 'volume': 1}]
    f.tables['app_config'] = [{'key': 'tax_cost_curve', 'value': json.dumps({'5': {'n': 3, 'mean_ret_pct': -1.0}})}]
    return f


def test_context_fy_from_statement():
    with patch.object(tc.db, 'supabase', _fake()):
        ctx = tc.context_for('primary', '2026-11-17', ['AAA', 'BBB'])
    assert ctx['fy'] == {'ytd_net_realised_inr': -47479.0, 'ltcg_used_fy': 0.0, 'fy_known': True}
    assert ctx['lots_by_symbol'] == {'AAA': [{'qty': 6, 'price': 100.0, 'date': '2025-01-01'},
                                             {'qty': 5, 'price': 120.0, 'date': '2025-06-01'}], 'BBB': []}
    assert ctx['curve'] == {'5': {'n': 3, 'mean_ret_pct': -1.0}}
    assert ctx['adv_by_symbol'] == {'AAA': 1000.0}


def test_context_unknown_fy_for_natwar():
    with patch.object(tc.db, 'supabase', _fake()):
        ctx = tc.context_for('natwar', '2026-11-17', ['AAA'])
    assert ctx['fy'] == {'ytd_net_realised_inr': 0.0, 'ltcg_used_fy': 0.0, 'fy_known': False}
    assert ctx['lots_by_symbol']['AAA'][0]['qty'] == 99


def test_fy_boundary_before_april():
    with patch.object(tc.db, 'supabase', _fake()):
        assert tc.context_for('primary', '2026-02-10', [])['fy']['ytd_net_realised_inr'] == 999.0   # FY25-26
        assert tc.context_for('primary', '2026-03-31', [])['fy']['fy_known'] is True
        assert tc.context_for('primary', '2024-09-10', [])['fy']['fy_known'] is False
