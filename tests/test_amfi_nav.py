"""AMFI NAV parser (item 3). Pure text parse — no network."""
import amfi_nav

SAMPLE = """Open Ended Schemes(Fund of Funds Scheme)

DSP Mutual Fund

Scheme Code;ISIN Div Payout/ ISIN Growth;ISIN Div Reinvestment;Scheme Name;Net Asset Value;Date
101206;INF740KA1EX9;;DSP Silver ETF Fund of Fund - Direct Plan - Growth;12.3456;09-Sep-2026
101207;INF740KA1EY7;;DSP Silver ETF Fund of Fund - Regular - IDCW;N.A.;09-Sep-2026

ICICI Prudential Mutual Fund

120591;INF109KC1AB2;;ICICI Prudential Silver ETF FOF - Direct - Growth;15.9900;09-Sep-2026
garbage line without semicolons
;;;;;
"""


def test_parses_valid_rows_and_skips_the_rest():
    rows = amfi_nav.parse_navall(SAMPLE)
    codes = {r['scheme_code'] for r in rows}
    assert codes == {'101206', '120591'}          # N.A. and headers skipped
    dsp = next(r for r in rows if r['scheme_code'] == '101206')
    assert dsp['nav'] == 12.3456
    assert dsp['nav_date'] == '2026-09-09'         # dd-Mon-YYYY -> ISO
    assert 'Silver' in dsp['scheme_name']


def test_na_nav_is_skipped_not_zeroed():
    rows = amfi_nav.parse_navall(SAMPLE)
    assert all(r['scheme_code'] != '101207' for r in rows)  # N.A. dropped


def test_empty_and_headeronly_input():
    assert amfi_nav.parse_navall('') == []
    assert amfi_nav.parse_navall('DSP Mutual Fund\n\nSome Category\n') == []
