from datetime import date, timedelta

import tax_actions as ta


def wd(a, b):
    d0, d1, n = date.fromisoformat(a), date.fromisoformat(b), 0
    while d0 < d1:
        d0 += timedelta(days=1)
        n += d0.weekday() < 5
    return n


def curve(mean40=-1.0, n=50):
    return {k: {'n': n, 'mean_ret_pct': mean40} for k in ('5', '10', '20', '40', '60')}


def row(**kw):
    r = dict(symbol='X', quantity=100, average_price=100.0, last_price=100.0, verdict='SELL')
    r.update(kw)
    return r


CTX = {'ytd_net_realised_inr': 0.0, 'ltcg_used_fy': 0.0, 'fy_known': True}
TODAY = '2026-11-17'


def test_fy_shield_zeroes_tax_and_names_it():
    ctx = dict(CTX, ytd_net_realised_inr=-47479.0)
    assert ta.fy_tax(30000.0, 0.0, ctx) == 0.0
    lots = [{'qty': 100, 'price': 100.0, 'date': '2026-09-01'}]
    p = ta.plan(row(last_price=400.0), lots, ctx, curve(), TODAY, None, wd)
    assert p['tax_now_inr'] == 0.0
    assert 'FY loss shield ₹47,479' in p['text']


def test_wait_when_saving_beats_drift():
    lots = [{'qty': 100, 'price': 100.0, 'date': '2025-12-20'}]
    p = ta.plan(row(quantity=100, average_price=100.0, last_price=700.0), lots, CTX,
                curve(-1.0), TODAY, None, wd)
    assert p['action'] == 'sell after 2026-12-21'
    assert p['ltcg_date'] == '2026-12-21'
    assert p['saving_inr'] > p['wait_cost_inr'] > 0


def test_no_wait_when_underwater():
    lots = [{'qty': 100, 'price': 100.0, 'date': '2025-12-20'}]
    p = ta.plan(row(last_price=80.0), lots, CTX, curve(), TODAY, None, wd)
    assert p['action'] == 'sell now'
    assert p['tax_now_inr'] == 0.0
    assert p['harvest_inr'] == 2000.0 and p['harvest_note']


def test_trim_sells_half_fifo():
    lots = [{'qty': 50, 'price': 100.0, 'date': '2024-01-01'},
            {'qty': 50, 'price': 200.0, 'date': '2026-10-01'}]
    p = ta.plan(row(verdict='TRIM', last_price=300.0, average_price=150.0), lots,
                CTX, curve(), TODAY, None, wd)
    # half = 50 shares = the old lot, all long-term: 50*200=10000 < exempt
    assert p['tax_now_inr'] == 0.0 and p['action'] == 'sell now'
    assert p['ltcg_date'] is None


def test_unknown_lots_flagged_short_term():
    lots = [{'qty': 60, 'price': 100.0, 'date': '2024-01-01'}]
    p = ta.plan(row(last_price=200.0), lots, CTX, curve(), TODAY, None, wd)
    assert p['lots'] == 'partial' and p['unmatched_qty'] == 40
    assert p['ltcg_date'] is None
    assert p['tax_now_inr'] == 40 * 100 * 0.20
    assert ta.plan(row(), [], CTX, curve(), TODAY, None, wd)['lots'] == 'unknown'


def test_curve_thin_means_no_wait():
    lots = [{'qty': 100, 'price': 100.0, 'date': '2025-12-20'}]
    p = ta.plan(row(last_price=700.0), lots, CTX, curve(-1.0, n=12), TODAY, None, wd)
    assert p['action'] == 'sell now'
    assert 'not enough history' in p['text']
    assert ta.expected_drop_pct(curve(-1.0, n=12), 31) is None


def test_hold_and_zero_qty():
    lots = [{'qty': 100, 'price': 100.0, 'date': '2025-12-20'}]
    p = ta.plan(row(verdict='HOLD', last_price=150.0), lots, CTX, curve(), TODAY, None, wd)
    assert p['action'] is None and p['text'] is None and p['ltcg_date'] == '2026-12-21'
    assert ta.plan(row(quantity=0), lots, CTX, curve(), TODAY, None, wd) is None


def test_expected_drop_interpolates_and_floors():
    c = curve()
    c['20'] = {'n': 50, 'mean_ret_pct': -2.0}
    c['40'] = {'n': 50, 'mean_ret_pct': -4.0}
    assert ta.expected_drop_pct(c, 30) == 3.0
    assert ta.expected_drop_pct(c, 3) == 1.0
    assert ta.expected_drop_pct(curve(+2.0), 30) == 0.0


def test_expected_drop_governing_checkpoint_with_thin_60():
    c = curve(-1.0)
    c['40'] = {'n': 50, 'mean_ret_pct': -4.0}
    c['60'] = {'n': 3, 'mean_ret_pct': -9.0}
    assert ta.expected_drop_pct(c, 60) == 4.0      # thin '60' skipped, '40' governs flat
    assert ta.expected_drop_pct(c, 59) == 4.0      # next checkpoint thin -> flat, not interpolated


def test_sell_qty_emitted_and_lots_suffix_both_branches():
    lots = [{'qty': 30, 'price': 100.0, 'date': '2024-01-01'}]
    p = ta.plan(row(quantity=100, last_price=200.0), lots, CTX, curve(), TODAY, None, wd)
    assert p['sell_qty'] == 100 and p['action'] == 'sell now'
    assert p['text'].endswith(' · lots: 30 of 100 dated')
    p = ta.plan(row(verdict='TRIM', quantity=100, last_price=200.0), [], CTX, curve(), TODAY, None, wd)
    assert p['sell_qty'] == 50 and ' · lots: unknown' in p['text']
    lots = [{'qty': 100, 'price': 100.0, 'date': '2025-12-20'}]
    p = ta.plan(row(last_price=700.0), lots, CTX, curve(-1.0), TODAY, None, wd)
    assert 'lots:' not in p['text']


def test_harvest_note_shown_on_underwater_sell_now():
    lots = [{'qty': 100, 'price': 100.0, 'date': '2025-12-20'}]
    p = ta.plan(row(last_price=80.0), lots, dict(CTX, ytd_net_realised_inr=5000.0), curve(), TODAY, None, wd)
    assert "realising -₹2,000 offsets ₹2,000 of this year's gains" in p['text']
    p = ta.plan(row(last_price=80.0), lots, CTX, curve(), TODAY, None, wd)
    assert 'realising −₹2,000 this FY (no realised gains on record)' in p['text']
    assert 'offsets ₹0' not in p['text']
    p = ta.plan(row(last_price=80.0), lots, dict(CTX, fy_known=False), curve(), TODAY, None, wd)
    assert 'no realised gains on record' in p['text']
