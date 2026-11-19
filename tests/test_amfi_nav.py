"""AMFI NAV parser (item 3). Pure text parse — no network."""
import amfi_nav

# Real AMFI layout: 8 columns (Scheme Name; Plan; Option before NAV; Date).
SAMPLE = """Open Ended Schemes(Fund of Funds Scheme)

DSP Mutual Fund

Scheme Code;ISIN Div Payout/ ISIN Growth;ISIN Div Reinvestment;Scheme Name;Plan;Option;Net Asset Value;Date
101206;INF740KA1EX9;-;DSP Silver ETF Fund of Fund;Direct Plan;Growth Option;12.3456;09-Sep-2026
101207;INF740KA1EY7;-;DSP Silver ETF Fund of Fund;Regular Plan;IDCW Option;N.A.;09-Sep-2026

ICICI Prudential Mutual Fund

120591;INF109KC1AB2;-;ICICI Prudential Silver ETF FOF;Direct Plan;Growth Option;15.9900;09-Sep-2026
garbage line without semicolons
;;;;;;;
"""


def test_parses_valid_rows_and_skips_the_rest():
    rows = amfi_nav.parse_navall(SAMPLE)
    codes = {r['scheme_code'] for r in rows}
    assert codes == {'101206', '120591'}          # N.A. and headers skipped
    dsp = next(r for r in rows if r['scheme_code'] == '101206')
    assert dsp['nav'] == 12.3456
    assert dsp['nav_date'] == '2026-09-09'         # dd-Mon-YYYY -> ISO
    assert dsp['scheme_name'] == 'DSP Silver ETF Fund of Fund - Direct Plan - Growth Option'


def test_na_nav_is_skipped_not_zeroed():
    rows = amfi_nav.parse_navall(SAMPLE)
    assert all(r['scheme_code'] != '101207' for r in rows)  # N.A. dropped


def test_empty_and_headeronly_input():
    assert amfi_nav.parse_navall('') == []
    assert amfi_nav.parse_navall('DSP Mutual Fund\n\nSome Category\n') == []


def test_fetch_held_keeps_only_mapped_schemes():
    """[P-38] storing all ~14.5k schemes a day made amfi_nav the biggest table
    (107 MB); every reader looks up holding_scheme_map codes only."""
    from unittest.mock import patch
    import amfi_nav
    import database as db
    db.supabase.table('holding_scheme_map').insert(
        [{'holding_label': 'A', 'amfi_scheme_code': '149039', 'confirmed': True},
         {'holding_label': 'B', 'amfi_scheme_code': '120304', 'confirmed': False}]).execute()
    rows = [{'scheme_code': c, 'scheme_name': c, 'nav': 1.0, 'nav_date': '2026-11-19'}
            for c in ('149039', '120304', '999999')]
    with patch.object(amfi_nav, 'fetch_and_parse', return_value=rows):
        kept = amfi_nav.fetch_held()
    assert sorted(r['scheme_code'] for r in kept) == ['120304', '149039']


def test_fetch_held_writes_nothing_without_a_map():
    from unittest.mock import patch
    import amfi_nav
    with patch.object(amfi_nav, 'fetch_and_parse', return_value=[{'scheme_code': '1'}]):
        assert amfi_nav.fetch_held() == []
