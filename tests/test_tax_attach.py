"""[P-68] tax_actions.attach: annotate only; never move a verdict."""
import copy

import tax_actions

TODAY = '2026-11-17'
CTX = {'lots_by_symbol': {'SELLME': [{'qty': 10, 'price': 100.0, 'date': '2026-03-01'}],
                          'HOLDME': [{'qty': 10, 'price': 100.0, 'date': '2026-01-01'}]},
       'fy': {'ytd_net_realised_inr': 0.0, 'fy_known': True}, 'curve': {}, 'adv_by_symbol': {}}


def _rows():
    return [{'symbol': 'SELLME', 'quantity': 10, 'average_price': 100.0, 'last_price': 150.0,
             'verdict': 'SELL', 'trend_score': -50, 'confidence': 70, 'stop_level': 90.0,
             'exit_target': 160.0, 'rotation_sell_qty': 5, 'rotation_target_symbol': 'X',
             'indicators': {'rsi': 40}, 'reasons': ['weak trend']},
            {'symbol': 'HOLDME', 'quantity': 10, 'average_price': 100.0, 'last_price': 150.0,
             'verdict': 'HOLD', 'trend_score': 60, 'reasons': []},
            {'symbol': 'GONE', 'quantity': 0, 'last_price': 1.0, 'verdict': 'SELL'}]


def _strip(rows):
    out = copy.deepcopy(rows)
    for r in out:
        (r.get('indicators') or {}).pop('tax', None)
        r.pop('reasons', None)
        if r.get('indicators') == {}:
            del r['indicators']
    return out


def test_attach_sets_indicators_tax_and_reason():
    rows = _rows()
    assert tax_actions.attach(rows, CTX, TODAY) == 2
    r = rows[0]
    assert r['indicators']['tax']['action'] in ('sell now',) or r['indicators']['tax']['action'].startswith('sell after')
    assert r['indicators']['rsi'] == 40
    assert r['reasons'][0] == 'weak trend' and r['reasons'][-1] == r['indicators']['tax']['text']
    assert 'tax' not in (rows[2].get('indicators') or {})


def test_verdict_and_rotation_untouched():
    before, after = _rows(), _rows()
    tax_actions.attach(after, CTX, TODAY)
    assert _strip(before) == _strip(after)


def test_hold_rows_only_get_ltcg_date():
    rows = _rows()
    tax_actions.attach(rows, CTX, TODAY)
    t = rows[1]['indicators']['tax']
    assert t['ltcg_date'] and t['action'] is None and t['text'] is None
    assert rows[1]['reasons'] == []


def test_nse_trading_days_skips_weekend():
    assert tax_actions.nse_trading_days('2026-11-13', '2026-11-16') == 1   # Fri -> Mon


def test_attach_isolates_bad_rows():
    rows = [dict(_rows()[0], symbol='SELLME'),
            {'symbol': 'SELLME', 'quantity': 10, 'average_price': 100.0, 'last_price': None,
             'verdict': 'SELL', 'indicators': None},
            {'symbol': 'SELLME', 'quantity': 10, 'average_price': 100.0, 'last_price': 150.0,
             'verdict': 'SELL', 'indicators': None, 'rotation_sell_qty': '5.0'}]
    assert tax_actions.attach(rows, CTX, TODAY) == 2
    assert 'tax' in rows[0]['indicators'] and 'tax' in rows[2]['indicators']
    assert 'tax' not in (rows[1].get('indicators') or {})
