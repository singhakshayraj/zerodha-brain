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
