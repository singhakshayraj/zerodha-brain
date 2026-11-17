import json
from datetime import date, timedelta
from unittest.mock import patch

import household_book as hb
from tests.fake_supabase import FakeSupabase

TODAY = date(2026, 11, 17)


def _fake(targets=None):
    f = FakeSupabase()
    adv = lambda s, q, avg, lp: {'run_id': 'r1', 'run_date': '2026-11-16', 'is_official': True,
                                 'created_at': '2026-11-16T09:20:00', 'symbol': s, 'quantity': q,
                                 'avg_price': avg, 'last_price': lp}
    f.tables['portfolio_advice'] = [adv('AAA', 100, 90, 100), adv('BBB', 50, 100, 80),
                                    {**adv('OLD', 1, 1, 1), 'run_id': 'r0', 'created_at': '2026-11-01T09:20:00'}]
    nat = {'rows': [{'symbol': 'AAA', 'quantity': 40, 'avg_price': 95, 'last_price': 100},
                    {'symbol': 'CCC', 'quantity': 10, 'average_price': 200, 'last_price': 210}]}
    f.tables['app_config'] = [{'key': 'advisor_natwar_latest', 'value': json.dumps(nat)},
                              {'key': 'tax_cost_curve', 'value': json.dumps({'5': {'n': 3, 'mean_ret_pct': -1.0}})}]
    if targets:
        f.tables['app_config'].append({'key': 'household_targets', 'value': json.dumps(targets)})
    f.tables['holding_scheme_map'] = [{'holding_label': 'Some Fund', 'confirmed': False},
                                      {'holding_label': 'Ok Fund', 'confirmed': True}]
    f.tables['pnl_daily'] = [{'account': a, 'date': f'2026-11-{d:02d}', 'pnl': 10.0 * d, 'value_open': 10000.0,
                              'nifty_pnl': 5.0 * d} for a in ('primary', 'natwar') for d in range(10, 15)]
    f.tables['tradebook'] = [
        {'account': 'primary', 'symbol': 'AAA', 'trade_type': 'buy', 'quantity': 100, 'price': 90,
         'trade_date': '2025-01-01', 'executed_at': 'a', 'trade_id': '1'},
        {'account': 'primary', 'symbol': 'BBB', 'trade_type': 'buy', 'quantity': 50, 'price': 100,
         'trade_date': '2025-06-01', 'executed_at': 'b', 'trade_id': '2'},
        {'account': 'natwar', 'symbol': 'AAA', 'trade_type': 'buy', 'quantity': 40, 'price': 95,
         'trade_date': '2025-03-01', 'executed_at': 'c', 'trade_id': '3'}]
    f.tables['pnl_statements'] = [{'account': 'primary', 'period_from': '2026-04-01', 'period_to': '2026-11-01',
                                   'summary': {'Realized P&L': 1000.0}}]
    f.tables['candles'] = [{'symbol': s, 'interval': 'day', 'trade_date': (date(2026, 10, 28) - timedelta(days=d)).isoformat(),
                            'close': 100.0 + (d % 5) * k + d * 0.1 * k,
                            'volume': 1000} for s, k in (('AAA', 1), ('BBB', -1), ('CCC', 2)) for d in range(0, 40)]
    return f


def _run(f):
    with patch.object(hb.db, 'supabase', f), patch.object(hb.tax_context.db, 'supabase', f), \
         patch.object(hb.benchmarks, 'sectors', return_value={'AAA': 'Financial Services', 'BBB': 'Information Technology'}), \
         patch.object(hb.benchmarks, 'size_buckets', return_value={'AAA': 'large'}), \
         patch.object(hb._h, '_index_weights', return_value={'Financial Services': 30.0}), \
         patch.object(hb, '_index_size_mix', return_value={'large': 70.0, 'mid': 20.0, 'small': 10.0}):
        return hb.publish(TODAY)


def test_publish_writes_book_and_default_targets():
    f = _fake()
    out = _run(f)
    cfg = {r['key']: json.loads(r['value']) for r in f.tables['app_config']}
    assert cfg['household_book_latest']['asof'] == '2026-11-17'
    assert cfg['household_targets'] == hb.DEFAULT_TARGETS and out['targets_source'] == 'default'
    assert out['net_worth']['by_account'] == {'primary': 14000.0, 'natwar': 6100.0, 'unknown': 0.0}
    assert out['funds_unconfirmed'] == ['Some Fund']
    assert set(out['returns']) == {'primary', 'natwar', 'household'}
    assert out['returns']['household']['start'] == '2026-11-10' and out['returns']['primary']['xirr'] is not None
    assert out['sections_asof'] == {'positions': '2026-11-16', 'returns': '2026-11-14',
                                    'tax': '2026-11-17', 'risk': '2026-10-28'}
    assert out['rebalance'] and out['tax']['by_account']['primary']
    assert 'OLD' not in json.dumps(out['xray'])
    assert out['risk']['n_used'] == 3
    # only the two allowed writes
    assert [(t, o) for t, o, _ in f.calls if o != 'select'] == [('app_config', 'upsert')] * 2


def test_publish_keeps_operator_targets():
    mine = {**hb.DEFAULT_TARGETS, 'name_cap_pct': 50.0, 'source': 'operator'}
    f = _fake(mine)
    out = _run(f)
    cfg = {r['key']: json.loads(r['value']) for r in f.tables['app_config']}
    assert cfg['household_targets'] == mine and out['targets_source'] == 'operator'
    assert [o for t, o, _ in f.calls if o != 'select'] == ['upsert']   # book only
