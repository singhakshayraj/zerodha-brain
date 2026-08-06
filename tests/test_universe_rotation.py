"""Universe breadth for data collection (DATA_UNIVERSE_ROTATION_N).

Every session to date analysed the same ~46 names, so the decision dataset
re-sampled one narrow slice of the market. `rotating_universe_slice` widens it
by a bounded, sector-balanced, date-rotating draw from the Nifty 500 — these
tests pin the three properties that make that draw worth anything: it spans
sectors, it moves day to day, and it is stable within a day (so a mid-session
restart cannot change the universe underneath a running session).
"""
import os
from datetime import date
from unittest.mock import MagicMock, patch

with patch.dict(os.environ, {
    'SUPABASE_URL': 'https://fake.supabase.co',
    'SUPABASE_SERVICE_KEY': 'fake-key',
}):
    with patch('supabase.create_client', return_value=MagicMock()):
        import database  # noqa

from brain import TradingBrain

slice_ = TradingBrain.rotating_universe_slice


# The REAL Nifty 500 sector distribution (measured against prod 2026-08-07).
# A synthetic pool with a handful of even sectors hides the whole problem: the
# imbalance here is the point — Financial Services alone is 101 of 500.
_SECTORS = [
    ("Financial Services", 101), ("Capital Goods", 63), ("Healthcare", 49),
    ("Automobile and Auto Components", 38), ("Consumer Services", 29),
    ("Fast Moving Consumer Goods", 28), ("Information Technology", 27),
    ("Chemicals", 26), ("Metals & Mining", 18), ("Oil Gas & Consumable Fuels", 17),
    ("Power", 17), ("Consumer Durables", 16), ("Services", 14),
    ("Construction", 13), ("Construction Materials", 11), ("Realty", 11),
    ("Telecommunication", 10), ("Textiles", 5),
    ("Media Entertainment & Publication", 4), ("Diversified", 3),
]


def _pool():
    rows, token = [], 1000
    for si, (sector, count) in enumerate(_SECTORS):
        tag = f"S{si:02d}"          # index-based: sector initials collide
        for i in range(count):
            rows.append({'symbol': f'{tag}{i:03d}', 'instrument_token': token, 'sector': sector})
            token += 1
    return rows


def test_returns_exactly_n_and_no_duplicates():
    picked = slice_(_pool(), exclude=set(), n=40, day=date(2026, 8, 7))
    assert len(picked) == 40
    assert len({p['symbol'] for p in picked}) == 40


def test_spans_sectors_rather_than_flooding_the_biggest():
    # A naive draw would return 40 Financial Services names (101 of 172 rows).
    picked = slice_(_pool(), exclude=set(), n=40, day=date(2026, 8, 7))
    sectors = {p['sector'] for p in picked}
    assert len(sectors) == 20, f'only {len(sectors)} of 20 sectors represented'
    # and no single sector dominates the slice
    fin = sum(1 for p in picked if p['sector'] == 'Financial Services')
    assert fin <= 4, f"Financial Services took {fin}/40"


def test_rotates_day_to_day():
    a = {p['symbol'] for p in slice_(_pool(), set(), 40, date(2026, 8, 7))}
    b = {p['symbol'] for p in slice_(_pool(), set(), 40, date(2026, 8, 8))}
    assert a != b, "consecutive sessions drew the identical slice"
    # it should genuinely move, not shuffle one name
    assert len(a - b) >= 10


def test_stable_within_a_day():
    # A mid-session restart must not change the universe underneath the session.
    day = date(2026, 8, 7)
    a = [p['symbol'] for p in slice_(_pool(), set(), 40, day)]
    b = [p['symbol'] for p in slice_(_pool(), set(), 40, day)]
    assert a == b


def test_walks_the_index_over_consecutive_sessions():
    seen = set()
    for d in range(1, 13):                       # 12 sessions, as collected so far
        seen |= {p['symbol'] for p in slice_(_pool(), set(), 40, date(2026, 8, d))}
    # Far more names than the ~46 a fixed universe would ever have shown.
    assert len(seen) > 200, f"only reached {len(seen)} distinct names"


def test_excludes_names_already_in_the_universe():
    exclude = {'NSE:FIN000', 'NSE:CAP000', 'NSE:TEX000'}
    picked = slice_(_pool(), exclude=exclude, n=40, day=date(2026, 8, 7))
    assert not ({f"NSE:{p['symbol']}" for p in picked} & exclude)


def test_skips_rows_without_a_usable_token():
    pool = [{'symbol': 'AAA', 'instrument_token': 0, 'sector': 'X'},
            {'symbol': 'BBB', 'instrument_token': None, 'sector': 'X'},
            {'symbol': 'CCC', 'instrument_token': 7, 'sector': 'X'}]
    picked = slice_(pool, set(), 10, date(2026, 8, 7))
    assert [p['symbol'] for p in picked] == ['CCC']


def test_degenerate_inputs_are_inert():
    assert slice_([], set(), 40, date(2026, 8, 7)) == []
    assert slice_(_pool(), set(), 0, date(2026, 8, 7)) == []
    assert slice_(_pool(), set(), -5, date(2026, 8, 7)) == []
    # a malformed row must not take the session down
    assert slice_([None, {}, {'symbol': 'OK', 'instrument_token': 1, 'sector': None}],
                  set(), 5, date(2026, 8, 7))[0]['symbol'] == 'OK'


def test_asking_for_more_than_the_pool_holds_returns_the_pool():
    picked = slice_(_pool(), set(), 10_000, date(2026, 8, 7))
    assert len(picked) == len(_pool())
