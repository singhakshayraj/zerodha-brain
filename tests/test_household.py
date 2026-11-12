"""Household book: caps and duplicate rule decide where freed money goes."""
import household as hh

SECTOR = {'PNB': 'Financial Services', 'SBIN': 'Financial Services', 'TCS': 'Information Technology',
          'INFY': 'Information Technology', 'LT': 'Construction', 'ASHOKLEY': 'Automobile and Auto Components'}


def _guard(pos, iw=None):
    return hh.Guard(pos, SECTOR, iw or {})


def _scored(*items):
    return {s: {'symbol': s, 'score': sc, 'sector': SECTOR[s]} for s, sc in items}


def test_duplicate_over_3pct_skipped_for_next_best():
    g = _guard({'PNB': 5300, 'X': 94700})                     # PNB already 5.3%
    t, note = hh.pick(-60, 'Financial Services', _scored(('PNB', 95), ('SBIN', 80)), g, 1000)
    assert t['symbol'] == 'SBIN' and t['reason'] == 'same_sector'
    assert 'skipped PNB' in note and '5.3%' in note


def test_small_duplicate_allowed():
    g = _guard({'PNB': 1000, 'X': 99000})                     # 1% + 1% buy = 2% < 3%
    t, _ = hh.pick(-60, 'Financial Services', _scored(('PNB', 95)), g, 1000)
    assert t['symbol'] == 'PNB'


def test_sector_cap_blocks_and_underweight_preferred_cross_sector():
    g = _guard({'PNB': 19000, 'X': 81000}, iw={'Information Technology': 10.0, 'Construction': 0.0})
    # PSU banks at 19%: adding 2000 crosses 20% -> no same-sector target
    t, note = hh.pick(-60, 'Financial Services',
                      _scored(('SBIN', 99), ('LT', 90), ('TCS', 80)), g, 2000)
    assert t['symbol'] == 'TCS' and t['reason'] == 'cross_sector'   # IT underweight beats LT's higher score
    assert 'PSU banks' in note and 'underweight' in note


def test_commit_counts_earlier_suggestions_in_the_run():
    g = _guard({'A': 50000, 'B': 50000})
    t1, _ = hh.pick(-60, None, _scored(('ASHOKLEY', 90)), g, 2500)
    g.commit('A', 2500 / hh.DEPLOY, t1['symbol'], 2500)
    t2, note = hh.pick(-60, None, _scored(('ASHOKLEY', 90)), g, 2500)   # 2.5% + 2.5% > 3%
    assert t1['symbol'] == 'ASHOKLEY' and t2 is None and 'already' in note


def test_gate_unchanged_weak_exit_required():
    g = _guard({'X': 1})
    assert hh.pick(10, None, _scored(('TCS', 99)), g, 100) == (None, None)


def test_unknown_sector_bucket_is_not_capped():
    g = hh.Guard({'BIG': 1_000_000}, {}, {})
    t, _ = hh.pick(-30, None, {'ZZZ': {'symbol': 'ZZZ', 'score': 90, 'sector': None}}, g, 950)
    assert t['symbol'] == 'ZZZ'


def test_other_accounts_same_day_suggested_buys_count():
    rows = [{'symbol': 'SILVERBEES', 'quantity': 425, 'last_price': 176.0,
             'rotation_target_symbol': 'LTTS', 'rotation_buy_qty': 17, 'rotation_buy_price': 4249.0}]
    assert hh.add_rows({}, rows, pending=True) == {'SILVERBEES': 74800.0, 'LTTS': 72233.0}
    assert hh.add_rows({}, rows, pending=False) == {'SILVERBEES': 74800.0}
