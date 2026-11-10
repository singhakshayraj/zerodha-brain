import importlib.util
import os

_p = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'scripts', 'hero_picks.py')
_spec = importlib.util.spec_from_file_location('hero_picks', _p)
hp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hp)

PASTE = (
    "S.No.\tName\tCMP Rs.\tP/E\tMar Cap Rs.Cr.\tCash Eqv Rs.Cr.\tDebt Rs.Cr.\t"
    "CF Opr Rs.Cr.\tROCE %\tPiotski Scr\tPledged %\tEV / EBITDA\n"
    "1.\t3M India\t30,000\t40.5\t33,000\t1,500\t20\t600\t30.1\t7\t0.00\t25\n"
    "S.No.\tName\tCMP Rs.\tP/E\tMar Cap Rs.Cr.\tCash Eqv Rs.Cr.\tDebt Rs.Cr.\t"
    "CF Opr Rs.Cr.\tROCE %\tPiotski Scr\tPledged %\tEV / EBITDA\n"
    "2.\tUnknown Co\t10\t5\t100\t1\t2\t3\t4\t5\t6\t7\n"
)


def test_parse_maps_columns_and_numbers_skipping_repeated_headers():
    rows = hp.parse_paste(PASTE)
    assert [r['name'] for r in rows] == ['3M India', 'Unknown Co']
    r = rows[0]
    assert (r['mcap'], r['pe'], r['cash'], r['debt'], r['pledge']) == \
           (33000.0, 40.5, 1500.0, 20.0, 0.0)
    assert r['ev_ebitda'] == 25.0


def test_missing_required_column_is_named():
    try:
        hp.parse_paste("Name\tP/E\nX\t1\n")
        assert False
    except ValueError as e:
        assert 'cash' in str(e)


def test_match_symbols_by_company_name():
    uni = {'3MINDIA': {'company_name': '3M India Ltd.', 'sector': 'Diversified'}}
    m, un = hp.match_symbols(hp.parse_paste(PASTE), uni)
    assert m[0]['symbol'] == '3MINDIA' and un == ['Unknown Co']


def _closes(path):
    return [(f'd{i:04d}', v) for i, v in enumerate(path)]


def test_price_metrics_and_gates():
    # index flat; stock falls 50% over first 2y, bottoms 100 bars ago, recovers a bit
    idx = _closes([100.0] * 760)
    stock = [100.0] * 260 + [100 - i * 0.2 for i in range(250)]   # down to ~50.2
    stock += [50.0] * 150 + [50 + i * 0.05 for i in range(100)]    # low ~250..100 bars ago
    m = hp.price_metrics(_closes(stock), idx)
    assert m['dd_3y'] < -0.35
    assert m['low_age_days'] >= 63
    r = {**m, 'cash': 10, 'debt': 1, 'cfo': 5, 'roce': 20, 'piotroski': 7, 'pledge': 0}
    assert all(hp.gates(r).values())
    r['pledge'] = 12
    assert hp.gates(r)['good_business'] is False


def test_rank_caps_per_sector():
    rows = [{'pe': 10 + i, 'cash': 100, 'debt': 0, 'mcap': 1000, 'sector': 'IT',
             'ev_ebitda': None} for i in range(5)]
    picks = hp.rank(rows)
    assert len(picks) == hp.PER_SECTOR
