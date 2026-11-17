from lab_book import Book, Position
from paper_broker import _zerodha_intraday_charges


def test_long_round_trip_costs():
    b = Book('T')
    p = b.try_open('X', 'LONG', ltp=1000.0, stop=990.0, target=None, slip_bps=5, ts='t0')
    assert p.qty == 25 and abs(p.entry_price - 1000.5) < 1e-9          # +5 bps
    row = b.close(p, ltp=1010.0, ts='t1', reason='EOD')
    assert abs(row['exit_price'] - 1009.495) < 1e-6                     # -5 bps
    assert abs(row['gross_inr'] - 250.0) < 1e-6                         # reference prices
    assert row['net_inr'] == round(row['gross_inr'] - row['charges_inr'] - row['slippage_inr'], 2)
    assert abs(row['gross_r'] - 250.0 / (10.5 * 25)) < 1e-6
    expected_charges = (_zerodha_intraday_charges('BUY', 1000.5, 25)
                        + _zerodha_intraday_charges('SELL', 1009.495, 25))
    assert abs(row['charges_inr'] - expected_charges) < 0.011
    assert p not in b.open


def test_margin_cap_boundary():
    b = Book('T')
    for i in range(10):
        assert isinstance(b.try_open(f'S{i}', 'LONG', 2500.0, 2475.0, None, 5, 't'), Position)
    assert b.try_open('S10', 'LONG', 2500.0, 2475.0, None, 5, 't') == 'margin'


def test_symbol_limit_three_per_day():
    b = Book('T')
    for _ in range(3):
        b.close(b.try_open('X', 'LONG', 100.0, 99.0, None, 5, 't'), 100.0, 't', 'EOD')
    assert b.try_open('X', 'LONG', 100.0, 99.0, None, 5, 't') == 'symbol_limit'


def test_stop_fill_clamped_to_quarter_r():
    b = Book('T')
    p = b.try_open('X', 'LONG', 100.0, 99.0, None, 5, 't')
    assert b.check_exit(p, 98.0) == 'STOP'
    row = b.close(p, 98.0, 't', 'STOP')
    assert abs(row['exit_price'] - (99.0 - 0.25 * (p.entry_price - 99.0))) < 1e-9


def test_short_mirrors_long():
    b = Book('T')
    p = b.try_open('X', 'SHORT', 100.0, 101.0, 98.0, 5, 't')
    assert b.check_exit(p, 98.0) == 'TARGET' and b.check_exit(p, 101.0) == 'STOP'
    assert b.check_exit(p, 100.0) is None
    assert b.close(p, 98.0, 't', 'TARGET')['gross_inr'] > 0


def test_qty_refusal_for_expensive_stock():
    assert Book('T').try_open('MRF', 'LONG', 130000.0, 129000.0, None, 5, 't') == 'qty'


def test_exchange_rate_is_0_00307_pct():
    # ₹1,00,000 SELL: brokerage 20, STT 25, exch 3.07, sebi 0.1, gst 0.18*(23.17)
    assert _zerodha_intraday_charges('SELL', 1000.0, 100) == round(20 + 25 + 3.07 + 0.1 + 0.18 * 23.17, 2)
