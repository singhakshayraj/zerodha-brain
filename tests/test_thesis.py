"""Thesis engine (P-47, shadow). Pure rules; v1 verdict untouched."""
import fundamentals
import thesis

TODAY = '2026-11-11'
GOOD = {'roce': 22, 'piotroski': 7, 'cash': 100, 'debt': 10, 'cfo': 50,
        'pledge': 0, 'pe': 15, 'asof': TODAY}
WEAK = {'roce': 5, 'piotroski': 3, 'cash': 10, 'debt': 100, 'cfo': -5,
        'pledge': 20, 'pe': 60, 'asof': TODAY}


def _h(symbol='AAA', qty=100, pnl=0.0, w=5.0, sector='Chemicals', mom=10.0):
    return {'symbol': symbol, 'quantity': qty, 'pnl_percent': pnl,
            'weight_pct': w, 'sector': sector, 'mom_12_1': mom}


def test_quality_score_bounds_and_missing():
    assert thesis.quality_score(GOOD) == 100
    assert thesis.quality_score(WEAK) == 0
    assert thesis.quality_score({'roce': 22}) is None          # <3 fields
    assert thesis.quality_score(None) is None
    # financials: ROE replaces ROCE, cash/debt skipped
    fin = {'roe': 18, 'piotroski': 6, 'cfo': 1, 'pledge': 0, 'pe': 10, 'asof': TODAY}
    assert thesis.quality_score(fin, 'Financial Services') == int(round((18 + 15 + 15 + 10 + 10) / 80 * 100))


def test_loss_breach_without_intact_business_exits():
    t = thesis.thesis_for(_h(pnl=-25), None, TODAY)
    assert (t['action'], t['rule'], t['qty_delta']) == ('EXIT', 'max_loss', -100)
    assert t['confidence'] == 'low'
    t = thesis.thesis_for(_h(pnl=-25), WEAK, TODAY)
    assert t['action'] == 'EXIT'


def test_loss_breach_with_intact_business_holds():
    t = thesis.thesis_for(_h(pnl=-25), GOOD, TODAY)
    assert t['action'] == 'HOLD' and 'inside the loss budget' in t['reason']


def test_weak_business_exit_or_trim_by_momentum():
    assert thesis.thesis_for(_h(mom=-5), WEAK, TODAY)['action'] == 'EXIT'
    t = thesis.thesis_for(_h(mom=5), WEAK, TODAY)
    assert (t['action'], t['qty_delta']) == ('TRIM', -50)


def test_single_name_cap_sizes_the_trim():
    t = thesis.thesis_for(_h(w=13.3, qty=425), GOOD, TODAY)
    assert t['action'] == 'TRIM' and t['target_weight_pct'] == 10.0
    assert t['qty_delta'] == int(round(425 * (10 / 13.3 - 1)))


def test_add_needs_quality_freshness_momentum_and_room():
    assert thesis.thesis_for(_h(w=2.0, qty=40), GOOD, TODAY)['action'] == 'ADD'
    stale = {**GOOD, 'asof': '2026-01-01'}
    assert thesis.thesis_for(_h(w=2.0), stale, TODAY)['action'] == 'HOLD'
    assert thesis.thesis_for(_h(w=2.0, mom=-1), GOOD, TODAY)['action'] == 'HOLD'
    assert thesis.thesis_for(_h(w=6.0), GOOD, TODAY)['action'] == 'HOLD'


def test_etf_is_allocation_only():
    t = thesis.thesis_for(_h('SILVERBEES', w=13.3, qty=425), None, TODAY)
    assert t['action'] == 'TRIM' and t['quality'] is None and t['confidence'] == 'normal'
    assert thesis.thesis_for(_h('HDFCSENSEX', w=6.9), None, TODAY)['action'] == 'HOLD'


def test_sector_cap_trims_lowest_quality_first():
    store = {'by_symbol': {'A': GOOD, 'B': {**GOOD, 'roce': 12}}}   # B weaker
    hs = [_h('A', w=15.0, sector='Power'), _h('B', w=15.0, sector='Power'),
          _h('C', w=5.0, sector='IT')]
    out = thesis.thesis_book(hs, store, TODAY)
    # A and B first hit the 10% single-name cap -> 20% sector, under 25%: no sector trim
    assert out['A']['rule'] == out['B']['rule'] == 'single_name_cap'
    hs = [_h('A', w=9.0, sector='Power'), _h('B', w=9.0, sector='Power'),
          _h('D', w=9.0, sector='Power', mom=None)]
    store['by_symbol']['D'] = None
    out = thesis.thesis_book(hs, store, TODAY)
    assert out['D']['rule'] == 'sector_cap'                      # no fundamentals = lowest
    assert out['D']['target_weight_pct'] == 7.0 and out['D']['action'] == 'TRIM'
    assert out['A']['action'] == 'HOLD'


def test_attach_is_additive_only():
    rows = [{'symbol': 'AAA', 'quantity': 10, 'pnl_percent': 1.0, 'verdict': 'SELL',
             'trend_score': -90, 'indicators': {'portfolio_weight_pct': 3.0, 'mom_12_1': 4.0}}]
    before = {k: v for k, v in rows[0].items() if k != 'indicators'}
    n = thesis.attach_theses(rows, {'by_symbol': {'AAA': GOOD}}, {'AAA': 'IT'}, TODAY)
    assert n == 1 and rows[0]['indicators']['thesis']['action'] == 'ADD'
    assert {k: v for k, v in rows[0].items() if k != 'indicators'} == before
    assert rows[0]['indicators']['portfolio_weight_pct'] == 3.0


PASTE = ("S.No.\tName\tCMP Rs.\tP/E\tMar Cap Rs.Cr.\tCash Eqv Rs.Cr.\tDebt Rs.Cr.\t"
         "CF Opr Rs.Cr.\tROCE %\tROE %\tPiotski Scr\tPledged %\tDebt / Eq\n"
         "1.\tInfosys\t1,500\t22.5\t6,20,000\t30,000\t8,000\t25,000\t40.1\t31.2\t7\t0.00\t0.05\n"
         "2.\tMade Up Co\t10\t5\t100\t1\t2\t3\t4\t5\t5\t6\t1\n")


def test_fundamentals_parse_match_merge_stale():
    rows = fundamentals.parse_paste(PASTE, required=['name', 'roce'])
    assert rows[0]['roce'] == 40.1 and rows[0]['roe'] == 31.2 and rows[0]['debt'] == 8000.0
    uni = {'INFY': {'company_name': 'Infosys Ltd.', 'sector': 'Information Technology'}}
    m, un = fundamentals.match_symbols(rows, uni)
    assert m[0]['symbol'] == 'INFY' and un == ['Made Up Co']
    store = fundamentals.merge_store({'by_symbol': {'TCS': {'roce': 1, 'asof': '2026-01-01'}}}, m, TODAY)
    assert set(store['by_symbol']) == {'TCS', 'INFY'}
    assert store['by_symbol']['INFY']['asof'] == TODAY and 'name' in store['by_symbol']['INFY']
    assert fundamentals.is_stale(store['by_symbol']['TCS'], TODAY)
    assert not fundamentals.is_stale(store['by_symbol']['INFY'], TODAY)


def test_quality_uses_extended_fields_when_present():
    base = {'roce': 25, 'piotroski': 8, 'cash': 100, 'debt': 10, 'cfo': 50, 'pledge': 0, 'pe': 15}
    assert thesis.quality_score(base) == 100
    growing = {**base, 'sales_growth_3y': 20, 'profit_growth_3y': 18, 'opm': 20, 'opm_last_year': 19,
               'interest_coverage': 12, 'fii_change': 1.0, 'dii_change': 0.5}
    assert thesis.quality_score(growing) == 100
    shrinking = {**base, 'sales_growth_3y': -5, 'profit_growth_3y': -10, 'opm': 15, 'opm_last_year': 20,
                 'interest_coverage': 1.0, 'fii_change': -3.0, 'dii_change': -1.0}
    assert thesis.quality_score(shrinking) == int(round(100 / 130 * 100))   # 77: core intact, all extended parts zero
