"""Field-level merge: a second paste with other columns extends a record."""
import fundamentals as f


def test_merge_store_adds_fields_without_dropping_existing():
    base = f.merge_store({}, [{'symbol': 'X', 'name': 'X', 'roce': 20.0, 'debt': 5.0}], '2026-11-11')
    ext = f.merge_store(base, [{'symbol': 'X', 'name': 'X', 'sales_growth_3y': 9.0, 'debt': None}], '2026-11-11')
    assert ext['by_symbol']['X'] == {'roce': 20.0, 'debt': 5.0, 'sales_growth_3y': 9.0,
                                     'asof': '2026-11-11', 'name': 'X'}


def test_extended_headers_do_not_collide():
    hdr = ['S.No.', 'Company', 'Symbol', 'Avg ROCE 5Y %', 'Debtor days', 'OPM % last year', 'FII holding %', 'Change FII holding (~3Y, pts)']
    cols = f.map_headers(hdr)
    assert 'roce' not in cols and 'debt' not in cols and 'opm' not in cols
    assert cols['avg_roce_5y'] == 3 and cols['debtor_days'] == 4 and cols['fii'] == 6 and cols['fii_change'] == 7
