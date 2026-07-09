"""PaperBroker — the paper-trading money path (fills, slippage, real
Zerodha charges folded into price). Previously had zero test coverage."""
import os
from unittest.mock import MagicMock, patch

with patch.dict(os.environ, {
    'SUPABASE_URL': 'https://fake.supabase.co',
    'SUPABASE_SERVICE_KEY': 'fake-key',
}):
    with patch('supabase.create_client', return_value=MagicMock()):
        import database  # noqa

import config
from paper_broker import PaperBroker, _zerodha_intraday_charges


def _kite(ltp=100.0):
    kite = MagicMock()
    if ltp is None:
        kite.get_ltp.return_value = {}
    else:
        kite.get_ltp.return_value = {'NSE:INFY': {'last_price': ltp}}
    return kite


def _broker():
    b = PaperBroker()
    b.session_id = None
    return b


# --- charges ---

def test_charges_stt_only_on_sell():
    buy = _zerodha_intraday_charges('BUY', 100.0, 10)
    sell = _zerodha_intraday_charges('SELL', 100.0, 10)
    assert sell > buy   # STT (0.025%) only hits the sell leg


def test_charges_brokerage_capped_at_20():
    # huge turnover — 0.03% would blow past ₹20, must be capped
    charges = _zerodha_intraday_charges('BUY', 5000.0, 10_000)
    turnover = 5000.0 * 10_000
    uncapped_brokerage = turnover * 0.0003
    assert uncapped_brokerage > 20.0
    # brokerage component alone can't exceed 20 — total must be less than
    # what an uncapped calc would give
    assert charges < uncapped_brokerage


def test_charges_scale_with_turnover():
    small = _zerodha_intraday_charges('BUY', 100.0, 1)
    large = _zerodha_intraday_charges('BUY', 100.0, 100)
    assert large > small


# --- fills: slippage direction ---

def test_buy_fill_price_above_ltp():
    b = _broker()
    result = b.place_buy_order(_kite(ltp=100.0), 'NSE:INFY', 'NSE', 10)
    assert result is not None
    assert result['price'] > 100.0   # adverse: buy pays up


def test_sell_fill_price_below_ltp():
    b = _broker()
    result = b.place_sell_order(_kite(ltp=100.0), 'NSE:INFY', 'NSE', 10)
    assert result is not None
    assert result['price'] < 100.0   # adverse: sell receives less


def test_short_fill_behaves_like_sell():
    b = _broker()
    result = b.place_short_order(_kite(ltp=100.0), 'NSE:INFY', 'NSE', 10)
    assert result['price'] < 100.0


def test_cover_fill_behaves_like_buy():
    b = _broker()
    result = b.cover_short_order(_kite(ltp=100.0), 'NSE:INFY', 'NSE', 10)
    assert result['price'] > 100.0


def test_fill_value_matches_price_times_quantity():
    b = _broker()
    result = b.place_buy_order(_kite(ltp=100.0), 'NSE:INFY', 'NSE', 10)
    assert result['value'] == result['price'] * 10
    assert result['quantity'] == 10
    assert result['status'] == 'COMPLETE'


# --- no live price: fail-closed, then hint-price fallback ---

def test_fill_fails_without_price_or_hint():
    b = _broker()
    result = b.place_buy_order(_kite(ltp=None), 'NSE:INFY', 'NSE', 10)
    assert result is None


def test_fill_uses_hint_price_when_ltp_unavailable():
    b = _broker()
    result = b.place_buy_order(_kite(ltp=None), 'NSE:INFY', 'NSE', 10, hint_price=200.0)
    assert result is not None
    assert result['price'] > 200.0   # slippage/charges applied on top of the hint


def test_fill_ignores_zero_or_negative_ltp():
    kite = MagicMock()
    kite.get_ltp.return_value = {'NSE:INFY': {'last_price': -5.0}}
    b = _broker()
    result = b.place_buy_order(kite, 'NSE:INFY', 'NSE', 10)
    assert result is None


def test_fill_logs_order_failed_activity_when_session_id_set():
    b = _broker()
    b.session_id = 'sess-1'
    with patch('paper_broker.db.log_brain_activity') as log:
        b.place_buy_order(_kite(ltp=None), 'NSE:INFY', 'NSE', 10)
    log.assert_called_once()
    assert log.call_args.args[1] == 'ORDER_FAILED'


# --- square_off_all: SHORTs must be covered, not sold ---

def test_square_off_all_covers_shorts_not_sells():
    b = _broker()
    trades = [
        {'symbol': 'NSE:INFY', 'exchange': 'NSE', 'quantity': 10, 'position_type': 'SHORT'},
    ]
    with patch.object(b, 'cover_short_order') as cover, \
         patch.object(b, 'place_sell_order') as sell:
        b.square_off_all(_kite(), trades)
    cover.assert_called_once()
    sell.assert_not_called()


def test_square_off_all_sells_longs():
    b = _broker()
    trades = [
        {'symbol': 'NSE:INFY', 'exchange': 'NSE', 'quantity': 10, 'position_type': 'LONG'},
    ]
    with patch.object(b, 'cover_short_order') as cover, \
         patch.object(b, 'place_sell_order') as sell:
        b.square_off_all(_kite(), trades)
    sell.assert_called_once()
    cover.assert_not_called()


def test_square_off_all_mixed_positions_route_correctly():
    b = _broker()
    trades = [
        {'symbol': 'NSE:INFY', 'exchange': 'NSE', 'quantity': 10, 'position_type': 'LONG'},
        {'symbol': 'NSE:TCS', 'exchange': 'NSE', 'quantity': 5, 'position_type': 'SHORT'},
    ]
    with patch.object(b, 'cover_short_order') as cover, \
         patch.object(b, 'place_sell_order') as sell:
        b.square_off_all(_kite(), trades)
    assert sell.call_count == 1
    assert cover.call_count == 1
    assert sell.call_args.args[1] == 'NSE:INFY'
    assert cover.call_args.args[1] == 'NSE:TCS'
