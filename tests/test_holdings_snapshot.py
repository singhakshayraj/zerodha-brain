"""Holdings snapshot capture + advisor_risk snapshot loader (Task 2)."""
import os, sys
from unittest.mock import MagicMock, patch
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import holdings_snapshot as hs
import advisor_risk as r


def _md(holdings):
    md = MagicMock()
    md.kite.get_holdings.return_value = holdings
    return md


def test_build_rows_listed_plus_confirmed_funds():
    md = _md([{'tradingsymbol': 'INFY', 'isin': 'INE009A01021',
               'quantity': 10, 'average_price': 1400, 'last_price': 1500}])
    with patch.object(hs.db, 'get_confirmed_holding_map',
                      return_value=[{'amfi_scheme_code': '120591', 'units': 250}]):
        sid, rows = hs.build_rows(md, source='test')
    assert len({r_['snapshot_id'] for r_ in rows}) == 1           # one capture
    listed = [x for x in rows if x['instrument_type'] == 'listed']
    fund = [x for x in rows if x['instrument_type'] == 'fund']
    assert listed[0]['identifier'] == 'INFY' and listed[0]['last_price'] == 1500
    assert fund[0]['identifier'] == '120591' and fund[0]['last_price'] is None
    assert fund[0]['quantity'] == 250


def test_fail_loud_writes_nothing_on_token_error():
    md = MagicMock()
    md.kite.get_holdings.side_effect = RuntimeError('Token expired')
    inserted = []
    with patch.object(hs.db, 'insert_holdings_snapshot',
                      side_effect=lambda rows: inserted.extend(rows)):
        import pytest
        with pytest.raises(RuntimeError):
            hs.capture_snapshot(md)
    assert inserted == []                                          # nothing written


def test_empty_book_refuses_to_write():
    md = _md([])
    with patch.object(hs.db, 'get_confirmed_holding_map', return_value=[]):
        import pytest
        with pytest.raises(RuntimeError):
            hs.build_rows(md)


def test_capture_is_single_insert():
    md = _md([{'tradingsymbol': 'TCS', 'quantity': 5, 'last_price': 4000}])
    calls = {'n': 0}
    with patch.object(hs.db, 'get_confirmed_holding_map', return_value=[]), \
         patch.object(hs.db, 'insert_holdings_snapshot',
                      side_effect=lambda rows: calls.__setitem__('n', calls['n'] + 1) or len(rows)):
        res = hs.capture_snapshot(md)
    assert calls['n'] == 1                                         # atomic
    assert res['listed'] == 1 and res['funds'] == 0


def test_loader_values_listed_and_leaves_unmapped_fund_null():
    rows = [
        {'identifier': 'INFY', 'instrument_type': 'listed', 'quantity': 10, 'last_price': 1500},
        {'identifier': '120591', 'instrument_type': 'fund', 'quantity': 250, 'last_price': None},
        {'identifier': '999999', 'instrument_type': 'fund', 'quantity': 100, 'last_price': None},
    ]
    book = r.load_book_from_snapshot(rows, nav_by_scheme={'120591': 16.0})
    vals = {p['identifier']: p['value'] for p in book['positions']}
    assert vals['INFY'] == 15000.0
    assert vals['120591'] == 4000.0
    assert vals['999999'] is None                                 # unmapped -> null
    assert '999999' in book['unpriced']
    assert abs(sum(p['weight'] for p in book['positions'] if p['weight']) - 1.0) < 1e-9
