"""T2.1 — OrderManager unit tests. All Kite calls mocked.

Every test here runs with the live-order interlock ARMED, because that is the
only way to exercise the placement logic at all. The interlock itself is
tested separately below, and it is the reason these fixtures are explicit:
if `arm_live_orders` is ever removed, these tests fail loudly rather than
quietly placing something.
"""
import pytest
from unittest.mock import MagicMock, patch

import config
from order_manager import LiveOrderPathDisabled, OrderManager


@pytest.fixture(autouse=True)
def arm_live_orders():
    """The real-money path refuses by default (2026-08-27). Arm it for these
    unit tests only."""
    with patch.object(config, 'LIVE_ORDERS_ARMED', True), \
         patch.object(config, 'LIVE_ORDERS_ACK_DATE', '2026-08-27'):
        yield


@pytest.fixture
def om():
    return OrderManager()


# --- the interlock itself -------------------------------------------------

def test_live_orders_refused_by_default():
    """The default posture. PAPER_TRADING alone used to be the only thing
    between a simulation and a real order on a real account."""
    with patch.object(config, 'LIVE_ORDERS_ARMED', False), \
         patch.object(config, 'LIVE_ORDERS_ACK_DATE', ''):
        o = OrderManager()
        for call in (lambda: o.place_buy_order(MagicMock(), 'INFY', 'NSE', 1),
                     lambda: o.place_sell_order(MagicMock(), 'INFY', 'NSE', 1),
                     lambda: o.cover_short_order(MagicMock(), 'INFY', 'NSE', 1)):
            with pytest.raises(LiveOrderPathDisabled):
                call()


def test_arming_needs_both_flags():
    """One stale env var must not be enough."""
    o = OrderManager()
    for armed, ack in ((True, ''), (False, '2026-08-27')):
        with patch.object(config, 'LIVE_ORDERS_ARMED', armed), \
             patch.object(config, 'LIVE_ORDERS_ACK_DATE', ack):
            with pytest.raises(LiveOrderPathDisabled):
                o.place_buy_order(MagicMock(), 'INFY', 'NSE', 1)


def _kite(order_id='ORD001', status='COMPLETE', avg_price=1350.0, qty=10):
    kite = MagicMock()
    kite.place_order.return_value = order_id
    kite.get_order_status.return_value = {
        'status': status,
        'average_price': avg_price,
        'filled_quantity': qty,
    }
    return kite


# --- place_buy_order ---

def test_place_buy_order_complete_returns_dict(om):
    kite = _kite(order_id='BUY001', status='COMPLETE', avg_price=1350.0, qty=10)
    with patch('order_manager.time.sleep'):
        result = om.place_buy_order(kite, 'RELIANCE', 'NSE', 10)
    assert result is not None
    assert result['order_id'] == 'BUY001'
    assert result['price'] == 1350.0
    assert result['quantity'] == 10
    assert result['value'] == 13500.0


def test_place_buy_order_not_complete_returns_none(om):
    kite = _kite(order_id='BUY002', status='REJECTED', avg_price=0, qty=0)
    with patch('order_manager.time.sleep'):
        result = om.place_buy_order(kite, 'TCS', 'NSE', 5)
    assert result is None


def test_place_buy_order_no_order_id_returns_none(om):
    kite = MagicMock()
    kite.place_order.return_value = None
    with patch('order_manager.time.sleep'):
        result = om.place_buy_order(kite, 'INFY', 'NSE', 5)
    assert result is None
    kite.get_order_status.assert_not_called()


def test_place_buy_order_exception_propagates(om):
    """place_buy_order has no try/except — exceptions propagate to caller."""
    kite = MagicMock()
    kite.place_order.side_effect = Exception("network timeout")
    with patch('order_manager.time.sleep'):
        with pytest.raises(Exception, match="network timeout"):
            om.place_buy_order(kite, 'WIPRO', 'NSE', 3)


# --- _check_safety_sell ---

def test_check_safety_sell_mis_position_exists_returns_true(om):
    kite = MagicMock()
    kite.get_positions.return_value = {
        'net': [
            {'tradingsymbol': 'RELIANCE', 'product': 'MIS', 'quantity': 5}
        ]
    }
    assert om._check_safety_sell(kite, 'NSE:RELIANCE') is True


def test_check_safety_sell_no_mis_position_returns_false(om):
    kite = MagicMock()
    kite.get_positions.return_value = {
        'net': [
            {'tradingsymbol': 'RELIANCE', 'product': 'CNC', 'quantity': 10}
        ]
    }
    assert om._check_safety_sell(kite, 'NSE:RELIANCE') is False


def test_check_safety_sell_empty_positions_returns_false(om):
    kite = MagicMock()
    kite.get_positions.return_value = {'net': []}
    assert om._check_safety_sell(kite, 'NSE:HDFCBANK') is False


# --- place_sell_order ---

def test_place_sell_order_safety_fail_returns_none(om):
    kite = MagicMock()
    kite.get_positions.return_value = {'net': []}  # no MIS position
    with patch('order_manager.time.sleep'):
        result = om.place_sell_order(kite, 'NSE:TCS', 'NSE', 3)
    assert result is None
    kite.place_order.assert_not_called()


def test_place_sell_order_complete_returns_dict(om):
    kite = MagicMock()
    kite.get_positions.return_value = {
        'net': [{'tradingsymbol': 'SBIN', 'product': 'MIS', 'quantity': 7}]
    }
    kite.place_order.return_value = 'SELL001'
    kite.get_order_status.return_value = {
        'status': 'COMPLETE',
        'average_price': 500.0,
        'filled_quantity': 7,
    }
    with patch('order_manager.time.sleep'):
        result = om.place_sell_order(kite, 'NSE:SBIN', 'NSE', 7)
    assert result is not None
    assert result['order_id'] == 'SELL001'
    assert result['price'] == 500.0


# --- place_short_order ---

def test_place_short_order_complete_returns_dict(om):
    kite = _kite(order_id='SHORT001', status='COMPLETE', avg_price=2400.0, qty=4)
    with patch('order_manager.time.sleep'):
        result = om.place_short_order(kite, 'AXISBANK', 'NSE', 4)
    assert result is not None
    assert result['order_id'] == 'SHORT001'
    assert result['price'] == 2400.0
    assert result['quantity'] == 4
    # No safety check for shorts
    kite.get_positions.assert_not_called()


# --- square_off_all: SHORTs must be covered, not sold ---

def test_square_off_all_covers_shorts_not_sells(om):
    trades = [
        {'symbol': 'NSE:AXISBANK', 'exchange': 'NSE', 'quantity': 4, 'position_type': 'SHORT'},
    ]
    with patch.object(om, 'cover_short_order') as cover, \
         patch.object(om, 'place_sell_order') as sell:
        om.square_off_all(MagicMock(), trades)
    cover.assert_called_once()
    sell.assert_not_called()


def test_square_off_all_sells_longs(om):
    trades = [
        {'symbol': 'NSE:SBIN', 'exchange': 'NSE', 'quantity': 7, 'position_type': 'LONG'},
    ]
    with patch.object(om, 'cover_short_order') as cover, \
         patch.object(om, 'place_sell_order') as sell:
        om.square_off_all(MagicMock(), trades)
    sell.assert_called_once()
    cover.assert_not_called()
