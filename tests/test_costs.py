"""costs.py — round-trip cost + FIFO after-tax proceeds [P-68 phase 0]."""
import costs


def _t(symbol, qty, price, day, side='buy', account='primary'):
    return {'symbol': symbol, 'account': account, 'trade_type': side,
            'quantity': qty, 'price': price, 'trade_date': day}


def test_fifo_splits_lots_across_twelve_months():
    trades = [_t('X', 10, 100.0, '2025-10-01'), _t('X', 10, 120.0, '2026-03-01')]
    lots = costs.tax_lots(trades, 'X', 'primary')
    assert [(l['qty'], l['price'], l['date']) for l in lots] == \
        [(10, 100.0, '2025-10-01'), (10, 120.0, '2026-03-01')]
    r = costs.after_tax_pct(lots, 20, 150.0, '2026-11-16')
    # lot 1: held > 12 months -> LTCG 500; lot 2: STCG 300
    assert r['ltcg_inr'] == 500.0
    assert r['stcg_inr'] == 300.0
    # LTCG 500 is under the exemption -> 0 tax; STCG 300 at 20% = 60
    assert r['tax_inr'] == 60.0
    assert r['ltcg_date'] == '2027-03-02'          # long-term from the day after twelve months


def test_fifo_consumes_sells_before_sizing_lots():
    trades = [_t('X', 10, 100.0, '2025-10-01'), _t('X', 4, 130.0, '2025-12-01', 'sell'),
              _t('X', 10, 120.0, '2026-03-01')]
    lots = costs.tax_lots(trades, 'X', 'primary')
    assert [(l['qty'], l['date']) for l in lots] == [(6, '2025-10-01'), (10, '2026-03-01')]


def test_exemption_is_an_input():
    lots = [{'qty': 10, 'price': 100.0, 'date': '2025-01-01'}]
    full = costs.after_tax_pct(lots, 10, 200.0, '2026-11-16', ltcg_used_fy=125000.0)
    assert full['ltcg_inr'] == 1000.0
    assert full['tax_inr'] == 125.0          # 12.5% of 1000, exemption already used
    fresh = costs.after_tax_pct(lots, 10, 200.0, '2026-11-16')
    assert fresh['tax_inr'] == 0.0


def test_round_trip_worst_bucket_when_adv_unknown():
    assert costs.round_trip_cost_pct(None) == 1.0          # 0.1*2 STT + 0.40*2 impact
    assert costs.round_trip_cost_pct(60e7) == 0.3          # 0.2 + 0.05*2
    assert costs.round_trip_cost_pct(6e7) == 0.5


def test_net_pct_subtracts_tax_and_cost():
    lots = [{'qty': 100, 'price': 100.0, 'date': '2026-01-01'}]
    r = costs.after_tax_pct(lots, 100, 110.0, '2026-06-01', adv_inr=60e7)
    assert r['gross_pct'] == 10.0
    assert r['stcg_inr'] == 1000.0 and r['tax_inr'] == 200.0
    assert r['cost_inr'] == round(11000 * 0.3 / 100, 2)
    assert r['net_pct'] == round((1000 - 200 - r['cost_inr']) / 10000 * 100, 2)


def test_twelve_months_exactly_is_still_short_term():
    lots = [{'qty': 10, 'price': 100.0, 'date': '2025-11-16'}]
    on_day = costs.after_tax_pct(lots, 10, 200.0, '2026-11-16')
    assert on_day['stcg_inr'] == 1000.0 and on_day['ltcg_inr'] == 0.0 and on_day['ltcg_date'] == '2026-11-17'
    assert costs.after_tax_pct(lots, 10, 200.0, '2026-11-17')['ltcg_inr'] == 1000.0


def test_oversell_is_reported_not_hidden():
    lots = [{'qty': 10, 'price': 100.0, 'date': '2026-01-01'}]
    r = costs.after_tax_pct(lots, 25, 110.0, '2026-06-01')
    assert r['unmatched_qty'] == 15
    assert r['gross_pct'] == 10.0            # on the 10 matched shares, not inflated by 25
    assert costs.tax_lots([_t('X', 10, 100.0, '2026-01-01'), _t('X', 12, 120.0, '2026-02-01', 'sell')], 'X', 'primary') == []
