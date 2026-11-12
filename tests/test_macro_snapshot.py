"""Macro context rows: levels + 1/5/20-day moves per series, VIX change."""
import macro_snapshot as ms


def test_build_rows_moves_and_vix_change():
    days = [f'2026-10-{d:02d}' for d in range(1, 26)]
    nifty = {d: 100 + i for i, d in enumerate(days)}
    vix = {d: 15 + (i % 2) for i, d in enumerate(days)}
    crude = {d: 50 for d in days[-3:]}                      # short futures history
    rows = ms.build_rows({'nifty': nifty, 'vix': vix, 'crude': crude}, breadth={days[-1]: {'n': 480}})
    assert len(rows) == 25
    last = rows[-1]['data']
    assert last['nifty']['d1'] == round((124 / 123 - 1) * 100, 2)
    assert last['nifty']['d20'] == round((124 / 104 - 1) * 100, 2)
    assert last['vix']['chg'] == -1.0 and rows[0]['data']['vix']['chg'] is None
    assert last['crude']['d1'] == 0.0 and last['crude']['d5'] is None
    assert last['breadth'] == {'n': 480} and 'breadth' not in rows[0]['data']
    assert last['fii_net_cr'] is None


def test_merge_data_keeps_importer_fields_and_refreshes_market_fields():
    stored = {'nifty': {'close': 1}, 'fii_net_cr': 12563.33, 'fii': {'net': 12563.33}, 'fut': {'fii_net': -70413}}
    fresh = {'nifty': {'close': 2}, 'vix': {'close': 17}, 'fii_net_cr': None}
    m = ms.merge_data(stored, fresh)
    assert m['nifty']['close'] == 2 and m['vix']['close'] == 17
    assert m['fii_net_cr'] == 12563.33 and m['fii']['net'] == 12563.33 and m['fut']['fii_net'] == -70413
    assert ms.merge_data(None, fresh)['fii_net_cr'] is None
