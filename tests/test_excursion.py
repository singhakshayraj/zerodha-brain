"""MFE/MAE — max favorable / adverse excursion per trade (Tier-1 #2 capture).
The path a trade takes before exit: whether stops were too tight / targets too
far, which entry+exit prices alone can't reveal."""
import os
from unittest.mock import MagicMock, patch

with patch.dict(os.environ, {
    'SUPABASE_URL': 'https://fake.supabase.co',
    'SUPABASE_SERVICE_KEY': 'fake-key',
}):
    with patch('supabase.create_client', return_value=MagicMock()):
        import database  # noqa

from brain import TradingBrain


def _brain():
    b = TradingBrain.__new__(TradingBrain)
    b.session_id = 's1'
    b.consecutive_losses = 0
    b._last_loss_exit = {}
    b._excursion = {}
    b.session_stats = {'trades_executed': 1, 'total_pnl': 0.0,
                       'winning_trades': 0, 'losing_trades': 0}
    return b


def _long():
    # entry 100, stop 98 → risk 2/share, qty 10 → risk 20 ₹
    return {'id': 't1', 'symbol': 'INFY', 'exchange': 'NSE',
            'position_type': 'LONG', 'stop_loss_price': 98, 'target_price': 120,
            'quantity': 10, 'entry_value': 1000, 'entry_price': 100}


def _short():
    # entry 100, stop 102 → risk 2/share, qty 10 → risk 20 ₹
    return {'id': 't2', 'symbol': 'TCS', 'exchange': 'NSE',
            'position_type': 'SHORT', 'stop_loss_price': 102, 'target_price': 80,
            'quantity': 10, 'entry_value': 1000, 'entry_price': 100}


# --- _unrealized_r sign + scale ---

def test_unrealized_r_long_profit():
    assert _brain()._unrealized_r(_long(), 105) == 2.5   # +5*10 / 20


def test_unrealized_r_long_loss():
    assert _brain()._unrealized_r(_long(), 97) == -1.5   # -3*10 / 20


def test_unrealized_r_short_profit_when_price_falls():
    assert _brain()._unrealized_r(_short(), 95) == 2.5   # (100-95)*10 / 20


def test_unrealized_r_short_loss_when_price_rises():
    assert _brain()._unrealized_r(_short(), 103) == -1.5


def test_unrealized_r_none_without_entry_or_qty():
    b = _brain()
    assert b._unrealized_r({'entry_price': None, 'quantity': 10, 'stop_loss_price': 98}, 100) is None
    assert b._unrealized_r({'entry_price': 100, 'quantity': 0, 'stop_loss_price': 98}, 100) is None


# --- _update_excursion tracks both extremes across a path ---

def test_excursion_tracks_favorable_and_adverse():
    b = _brain()
    t = _long()
    for price in (100, 104, 97, 101, 108, 99):  # peak +4R at 108, trough -1.5R at 97
        b._update_excursion(t, price)
    exc = b._excursion['t1']
    assert exc['mfe_r'] == 4.0    # (108-100)*10/20
    assert exc['mae_r'] == -1.5   # (97-100)*10/20


def test_excursion_all_favorable_path_has_positive_mae_floor():
    b = _brain()
    t = _long()
    for price in (101, 103, 106):   # never went negative
        b._update_excursion(t, price)
    exc = b._excursion['t1']
    assert exc['mfe_r'] == 3.0
    assert exc['mae_r'] == 0.5     # worst was still +0.5R


# --- _excursion_fields folds the close price and consumes the entry ---

def test_excursion_fields_folds_close_and_pops():
    b = _brain()
    t = _long()
    b._update_excursion(t, 104)                 # peak +2R
    fields = b._excursion_fields(t, 96)          # close at -2R (new adverse extreme)
    assert fields == {'mfe_r': 2.0, 'mae_r': -2.0}
    assert 't1' not in b._excursion             # consumed


def test_excursion_fields_empty_when_untracked_and_unusable():
    b = _brain()
    assert b._excursion_fields({'id': 'x', 'entry_price': None, 'quantity': 0}, 100) == {}


# --- persisted into the close payload ---

def test_close_persists_mfe_mae():
    b = _brain()
    b.market_data = MagicMock()
    b.kite = MagicMock()
    t = _long()
    b._update_excursion(t, 112)   # peak +6R earlier in the trade
    b.order_manager = MagicMock()
    b.order_manager.place_sell_order.return_value = {
        'order_id': 'o1', 'price': 98.0, 'value': 980.0, 'quantity': 10}
    captured = {}
    with patch('brain.db.close_trade', side_effect=lambda tid, d: captured.update(d)), \
         patch('brain.db.update_stock_score'), \
         patch('brain.db.log_brain_activity'):
        b._execute_sell_by_trade(t, 98.0, 'STOP_LOSS_HIT')
    assert captured['mfe_r'] == 6.0     # preserved from the path peak
    assert captured['mae_r'] == -1.0    # close at 98 = -1R
