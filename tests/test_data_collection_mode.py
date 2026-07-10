"""DATA_COLLECTION_MODE — soft session stops become logged counterfactuals so
the paper session runs its full day; MARKET_CLOSED stays hard-enforced; the
mode is force-disabled outside paper trading (safety interlock)."""
import os
import pytest
from unittest.mock import patch, MagicMock
import config
from risk_manager import RiskManager

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
    b.consecutive_losses = 3   # breaker threshold reached
    b._would_stop_logged = set()
    b._time_stop_logged = set()
    b.session_stats = {'trades_executed': 5, 'total_pnl': -400.0}
    b._log_activity_safe = MagicMock()
    b.end_session = MagicMock()
    b._update_excursion = MagicMock()
    b._minutes_open = MagicMock(return_value=1.0)
    return b


def _open_long():
    # priced between stop and target so no stop/target/time-stop fires
    return {'id': 't1', 'symbol': 'INFY', 'position_type': 'LONG',
            'stop_loss_price': 90.0, 'target_price': 110.0}


@pytest.fixture
def rm():
    return RiskManager()


def _config(capital=25000.0, max_trades=10):
    return {
        'capitalDeployed': capital,
        'maxLossPercent': 5.0,
        'maxProfitPercent': 15.0,
        'maxTrades': max_trades,
    }


def _stats(total_pnl=0.0, trades=0, losses=0, unrealized=0.0):
    return {
        'total_pnl': total_pnl,
        'trades_executed': trades,
        'consecutive_losses': losses,
        'unrealized_pnl': unrealized,
    }


# --- flag off: soft stops still enforced (regression guard) ---

def test_max_trades_enforced_when_flag_off(rm):
    with patch.object(config, 'data_collection_active', return_value=False), \
         patch.object(rm, 'is_market_open', return_value=True):
        out = rm.check_session_limits(_stats(trades=10), _config(max_trades=10))
    assert out['can_trade'] is False
    assert out['reason'].startswith('MAX_TRADES_HIT')
    assert 'would_stop' not in out


def test_daily_stop_enforced_when_flag_off(rm):
    # -3R on 25000 @ 1% = -750
    with patch.object(config, 'data_collection_active', return_value=False), \
         patch.object(rm, 'is_market_open', return_value=True):
        out = rm.check_session_limits(_stats(total_pnl=-800.0), _config())
    assert out['can_trade'] is False
    assert out['reason'].startswith('DAILY_STOP_3R')


# --- flag on: soft stops become counterfactuals, trading continues ---

def test_max_trades_counterfactual_when_flag_on(rm):
    with patch.object(config, 'data_collection_active', return_value=True), \
         patch.object(rm, 'is_market_open', return_value=True):
        out = rm.check_session_limits(_stats(trades=10), _config(max_trades=10))
    assert out['can_trade'] is True
    assert out['would_stop'].startswith('MAX_TRADES_HIT')


def test_daily_stop_counterfactual_when_flag_on(rm):
    with patch.object(config, 'data_collection_active', return_value=True), \
         patch.object(rm, 'is_market_open', return_value=True):
        out = rm.check_session_limits(_stats(total_pnl=-800.0), _config())
    assert out['can_trade'] is True
    assert out['would_stop'].startswith('DAILY_STOP_3R')


def test_max_profit_counterfactual_when_flag_on(rm):
    # target = 15% of 25000 = 3750
    with patch.object(config, 'data_collection_active', return_value=True), \
         patch.object(rm, 'is_market_open', return_value=True):
        out = rm.check_session_limits(_stats(total_pnl=4000.0), _config())
    assert out['can_trade'] is True
    assert out['would_stop'].startswith('MAX_PROFIT_HIT')


# --- market close stays HARD even with the flag on ---

def test_market_closed_hard_enforced_when_flag_on(rm):
    with patch.object(config, 'data_collection_active', return_value=True), \
         patch.object(rm, 'is_market_open', return_value=False):
        out = rm.check_session_limits(_stats(trades=3), _config())
    assert out['can_trade'] is False
    assert out['reason'] == 'MARKET_CLOSED'
    assert 'would_stop' not in out


# --- clean run: no stop, no would_stop marker ---

def test_no_stop_no_marker(rm):
    with patch.object(config, 'data_collection_active', return_value=True), \
         patch.object(rm, 'is_market_open', return_value=True):
        out = rm.check_session_limits(_stats(trades=2), _config())
    assert out['can_trade'] is True
    assert 'would_stop' not in out


# --- safety interlock: mode only active in paper trading ---

def test_data_collection_active_requires_paper():
    with patch.object(config, 'DATA_COLLECTION_MODE', True), \
         patch.object(config, 'PAPER_TRADING', False):
        assert config.data_collection_active() is False
    with patch.object(config, 'DATA_COLLECTION_MODE', True), \
         patch.object(config, 'PAPER_TRADING', True):
        assert config.data_collection_active() is True


def test_assert_safe_boot_rejects_real_money_data_collection():
    with patch.object(config, 'DATA_COLLECTION_MODE', True), \
         patch.object(config, 'PAPER_TRADING', False), \
         patch.object(config, 'QA_MODE', False), \
         patch.dict('os.environ', {'REAL_TRADING_CONFIRM': 'I-UNDERSTAND-REAL-MONEY'}):
        with pytest.raises(RuntimeError, match='DATA_COLLECTION_MODE'):
            config.assert_safe_boot()


# --- circuit breaker: counterfactual under the flag, hard stop without ---

def test_circuit_breaker_ends_session_when_flag_off():
    b = _brain()
    with patch.object(config, 'data_collection_active', return_value=False), \
         patch('brain.db') as db:
        b._evaluate_exit(_open_long(), 100.0)
    b.end_session.assert_called_once_with('CIRCUIT_BREAKER')


def test_circuit_breaker_counterfactual_when_flag_on():
    b = _brain()
    with patch.object(config, 'data_collection_active', return_value=True), \
         patch('brain.db'):
        b._evaluate_exit(_open_long(), 100.0)
    b.end_session.assert_not_called()
    # logged once as a would-stop marker
    types = [c.args[0] for c in b._log_activity_safe.call_args_list]
    assert 'LIMIT_WOULD_STOP' in types
    assert 'CIRCUIT_BREAKER' in b._would_stop_logged


def test_circuit_breaker_counterfactual_logged_once():
    b = _brain()
    with patch.object(config, 'data_collection_active', return_value=True), \
         patch('brain.db'):
        b._evaluate_exit(_open_long(), 100.0)
        b._evaluate_exit(_open_long(), 100.0)
    types = [c.args[0] for c in b._log_activity_safe.call_args_list]
    assert types.count('LIMIT_WOULD_STOP') == 1
