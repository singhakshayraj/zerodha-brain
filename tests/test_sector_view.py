"""Sector view aggregates scanned names + holdings; display only."""
import sector_view as sv

SECTOR = {'SBIN': 'Financial Services', 'PNB': 'Financial Services', 'HDFCBANK': 'Financial Services',
          'TCS': 'Information Technology', 'INFY': 'Information Technology'}


def test_financials_split_into_subgroups():
    assert sv.group_of('PNB', 'Financial Services') == 'PSU banks'
    assert sv.group_of('HDFCBANK', 'Financial Services') == 'Private banks'
    assert sv.group_of('BAJFINANCE', 'Financial Services') == 'NBFC & other finance'
    assert sv.group_of('TCS', 'Information Technology') == 'Information Technology'


def test_aggregate_weights_mix_and_held():
    calls = [{'symbol': 'SBIN', 'verdict': 'SELL', 'trend_score': -30, 'quality': 60},
             {'symbol': 'TCS', 'verdict': 'HOLD', 'trend_score': 40, 'quality': 90}]
    holdings = [{'symbol': 'PNB', 'quantity': 100, 'last_price': 100.0, 'verdict': 'SELL', 'trend_score': -50},
                {'symbol': 'INFY', 'quantity': 10, 'last_price': 1000.0, 'verdict': 'HOLD', 'trend_score': 20}]
    store = {'by_symbol': {'SBIN': {'mcap': 700, 'gnpa': 2.0}, 'PNB': {'mcap': 100, 'gnpa': 4.0},
                           'TCS': {'mcap': 900}, 'INFY': {'mcap': 300}}}
    out = {g['group']: g for g in sv.aggregate(calls, holdings, store, SECTOR)}
    psu, it = out['PSU banks'], out['Information Technology']
    assert (psu['names'], psu['my_weight_pct'], psu['index_weight_pct']) == (2, 50.0, 40.0)
    assert psu['mix'] == {'sell': 2, 'trim': 0, 'hold': 0} and psu['sell_pct'] == 100
    assert psu['trend_median'] == -40 and psu['gnpa_median'] == 3.0
    assert [h['symbol'] for h in psu['held']] == ['PNB'] and psu['held'][0]['weight_pct'] == 50.0
    assert it['index_weight_pct'] == 60.0 and it['quality_median'] == 90


def test_etfs_and_unlisted_get_their_own_groups():
    assert sv.group_of('SILVERBEES', None) == 'ETFs (gold, silver, index)'
    assert sv.group_of('HDFCSENSEX', None) == 'ETFs (gold, silver, index)'
    assert sv.group_of('SUPRAJIT', None) == 'Outside Nifty 500'
