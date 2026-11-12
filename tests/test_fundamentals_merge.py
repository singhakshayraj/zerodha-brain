"""Field-level merge: a second paste with other columns extends a record."""
import fundamentals as f


def test_merge_store_adds_fields_without_dropping_existing():
    base = f.merge_store({}, [{'symbol': 'X', 'name': 'X', 'roce': 20.0, 'debt': 5.0}], '2026-11-11')
    ext = f.merge_store(base, [{'symbol': 'X', 'name': 'X', 'sales_growth_3y': 9.0}], '2026-11-11')
    assert ext['by_symbol']['X'] == {'roce': 20.0, 'debt': 5.0, 'sales_growth_3y': 9.0,
                                     'asof': '2026-11-11', 'name': 'X'}
    blank = f.merge_store(ext, [{'symbol': 'X', 'name': 'X', 'pe': None, 'roce': None}], '2026-12-01')
    assert blank['by_symbol']['X']['roce'] is None             # blank cell in a pasted column clears it
    assert blank['by_symbol']['X']['debt'] == 5.0              # column not in this paste untouched


def test_extended_headers_do_not_collide():
    hdr = ['S.No.', 'Company', 'Symbol', 'Avg ROCE 5Y %', 'Debtor days', 'OPM % last year', 'FII holding %', 'Change FII holding (~3Y, pts)']
    cols = f.map_headers(hdr)
    assert 'roce' not in cols and 'debt' not in cols and 'opm' not in cols
    assert cols['avg_roce_5y'] == 3 and cols['debtor_days'] == 4 and cols['fii'] == 6 and cols['fii_change'] == 7


def test_snapshot_rows_cover_only_touched_symbols():
    store = {'asof': '2026-11-11', 'by_symbol': {'A': {'roce': 1.0, 'asof': '2026-11-11', 'name': 'A'},
                                                 'B': {'roce': 2.0, 'asof': '2026-10-01', 'name': 'B'}}}
    rows = f.snapshot_rows(store, ['A', 'A', 'Z'], '2026-11-11')
    assert rows == [{'asof': '2026-11-11', 'symbol': 'A', 'data': {'roce': 1.0, 'name': 'A'}}]


def test_52w_distance_headers_do_not_grab_price_fields():
    cols = f.map_headers(['Company', 'Down from 52W high %', 'Up from 52W low %', 'High 52W', 'Low 52W'])
    assert cols['down_from_high'] == 1 and cols['up_from_low'] == 2
    assert cols['high_52w'] == 3 and cols['low_52w'] == 4
