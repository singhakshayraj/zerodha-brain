import sys
sys.path.insert(0, 'scripts')
import build_nifty500_tokens as b


def test_non_eq_series_maps_to_kite_suffixed_symbol():
    cons = [{'Symbol': 'HFCL', 'Series': 'BE', 'Industry': 'Telecommunication', 'Company Name': 'HFCL Ltd.'},
            {'Symbol': 'AAA', 'Series': 'EQ', 'Industry': 'IT', 'Company Name': 'A'}]
    inst = [{'segment': 'NSE', 'instrument_type': 'EQ', 'tradingsymbol': 'HFCL-BE', 'instrument_token': '5620225'},
            {'segment': 'NSE', 'instrument_type': 'EQ', 'tradingsymbol': 'AAA', 'instrument_token': '1'}]
    rows, missing = b.build_rows(cons, inst)
    assert missing == []
    assert [r['symbol'] for r in rows] == ['HFCL-BE', 'AAA']
