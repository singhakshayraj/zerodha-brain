"""universe_history — point-in-time Nifty 500 membership [P-68 phase 0]."""
import universe_history as uh

RELEASE = """
1) Nifty 50

No change.

2) Nifty 500

The following companies are being excluded:

Sr. No. Company Name Symbol
1 Alivus Life Sciences Ltd. ALIVUS
2 Gujarat Narmada Valley Fertilizers and Chemicals Ltd. GNFC


Sr. No. Company Name Symbol
3 GE T&D India Ltd. GET&D

The following companies are being included:

Sr. No. Company Name Symbol
1 Aditya Birla Lifestyle Brands Ltd. ABLBL
2 Hexaware Technologies Ltd. HEXT

3) Nifty 500 Multicap 50:25:25

The following companies are being excluded:

Sr. No. Company Name Symbol
1 Should Not Appear Ltd. NOPE
"""


def test_parse_release_reads_the_nifty_500_tables_only():
    r = uh.parse_release(RELEASE, released='2025-08-22')
    assert r['excluded'] == ['ALIVUS', 'GNFC', 'GET&D']
    assert r['included'] == ['ABLBL', 'HEXT']
    assert r['effective'] == '2025-09-30'          # August release -> end of September


def test_parse_release_uses_the_stated_effective_date():
    text = "changes will be effective from March 28, 2025 (close of March 27, 2025)\n" + RELEASE
    assert uh.parse_release(text, released='2025-02-21')['effective'] == '2025-03-28'


def test_members_excludes_a_name_after_it_left(tmp_path):
    csv = tmp_path / 'u.csv'
    csv.write_text('symbol,joined,left,status\n'
                   'OLD,,2023-09-29,trading\n'        # in the index since before our data, left 2023-09
                   'EVER,,,trading\n'                 # always in
                   'NEW,2025-03-28,,trading\n'        # joined 2025-03
                   'BRIEF,2022-09-30,2024-03-28,delisted\n')
    m = uh.members('2024-01-01', path=str(csv))
    assert m == {'EVER', 'BRIEF'}
    assert uh.members('2023-09-28', path=str(csv)) == {'OLD', 'EVER', 'BRIEF'}
    assert uh.members('2025-03-28', path=str(csv)) == {'EVER', 'NEW'}


def test_build_rows_from_events_and_pin():
    pin = {'EVER', 'NEW', 'HEXT'}
    events = [{'effective': '2025-03-28', 'included': ['NEW'], 'excluded': ['OLD']},
              {'effective': '2025-09-30', 'included': ['HEXT'], 'excluded': ['BRIEF']},
              {'effective': '2022-09-30', 'included': ['BRIEF'], 'excluded': []}]
    rows = {r['symbol']: r for r in uh.build_rows(pin, events)}
    assert rows['EVER'] == {'symbol': 'EVER', 'joined': '', 'left': ''}
    assert rows['NEW'] == {'symbol': 'NEW', 'joined': '2025-03-28', 'left': ''}
    assert rows['OLD'] == {'symbol': 'OLD', 'joined': '', 'left': '2025-03-28'}
    assert rows['BRIEF'] == {'symbol': 'BRIEF', 'joined': '2022-09-30', 'left': '2025-09-30'}


def test_rejoiner_gets_one_row_per_spell():
    pin = {'BACK'}
    events = [{'effective': '2023-09-29', 'included': [], 'excluded': ['BACK']},
              {'effective': '2025-03-28', 'included': ['BACK'], 'excluded': []}]
    rows = [r for r in uh.build_rows(pin, events) if r['symbol'] == 'BACK']
    assert rows == [{'symbol': 'BACK', 'joined': '', 'left': '2023-09-29'},
                    {'symbol': 'BACK', 'joined': '2025-03-28', 'left': ''}]
