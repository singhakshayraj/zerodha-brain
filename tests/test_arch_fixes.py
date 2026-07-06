"""Tests for the architecture-failure fixes (2026-07-07 scan):
transient-DB-error resilience, instance lock, idle-STOP teardown,
circuit-breaker resume, unrealized P&L in loss limit, zombie-trade guards,
startup interlocks."""
import os
import threading
from unittest.mock import MagicMock, patch

import pytest

with patch.dict(os.environ, {
    'SUPABASE_URL': 'https://fake.supabase.co',
    'SUPABASE_SERVICE_KEY': 'fake-key',
}):
    with patch('supabase.create_client', return_value=MagicMock()):
        import database  # noqa

import scheduler
from brain import TradingBrain
from risk_manager import RiskManager


# --- transient DB error must NOT look like an external stop ---

def test_session_still_active_fails_open_on_db_error():
    with patch('scheduler.db.get_config_strict', side_effect=RuntimeError('boom')), \
         patch('scheduler.time.sleep'):
        assert scheduler._session_still_active('sess-1') is True


def test_session_still_active_true_when_id_matches():
    with patch('scheduler.db.get_config_strict', return_value='sess-1'):
        assert scheduler._session_still_active('sess-1') is True


def test_session_still_active_false_on_positive_mismatch():
    with patch('scheduler.db.get_config_strict', return_value='other'):
        assert scheduler._session_still_active('sess-1') is False


def test_session_still_active_recovers_after_transient_error():
    with patch('scheduler.db.get_config_strict',
               side_effect=[RuntimeError('blip'), 'sess-1']), \
         patch('scheduler.time.sleep'):
        assert scheduler._session_still_active('sess-1') is True


# --- instance lock: only positive evidence of a newer instance counts ---

def test_lock_lost_when_newer_instance_claimed():
    with patch('scheduler.db.get_config_strict', return_value='someone-else'):
        assert scheduler._lock_lost() is True


def test_lock_not_lost_when_own_id():
    with patch('scheduler.db.get_config_strict', return_value=scheduler.INSTANCE_ID):
        assert scheduler._lock_lost() is False


def test_lock_not_lost_on_db_error_or_missing_key():
    with patch('scheduler.db.get_config_strict', side_effect=RuntimeError('boom')):
        assert scheduler._lock_lost() is False
    with patch('scheduler.db.get_config_strict', return_value=None):
        assert scheduler._lock_lost() is False


# --- SIGTERM exits without teardown ---

def test_sigterm_handler_raises_systemexit():
    with pytest.raises(SystemExit):
        scheduler._handle_sigterm(15, None)


# --- STOP while idle finalizes the orphaned session ---

def test_stop_while_idle_finalizes_orphaned_running_session():
    scheduler._is_trading = False
    cmds = ['STOP']

    def get_cmd():
        if cmds:
            return cmds.pop(0)
        raise KeyboardInterrupt

    writes = []

    with patch('scheduler.db.get_brain_command', side_effect=get_cmd), \
         patch('scheduler.db.get_config', return_value='ghost-sess-1'), \
         patch('scheduler.db.get_session_by_id',
               return_value={'id': 'ghost-sess-1', 'status': 'RUNNING'}), \
         patch('scheduler.db.end_session') as mock_end, \
         patch('scheduler.db.write_config',
               side_effect=lambda k, v: writes.append((k, v))), \
         patch('scheduler.db.update_heartbeat'), \
         patch('scheduler.token_refresher.maybe_daily_refresh'), \
         patch('scheduler.time.sleep'):
        try:
            scheduler.run()
        except (KeyboardInterrupt, StopIteration):
            pass

    mock_end.assert_called_once_with('ghost-sess-1', 'MANUAL_STOP')
    assert ('active_session_id', '') in writes
    assert ('brain_status', 'IDLE') in writes


def test_stop_while_idle_with_no_session_just_resets_status():
    scheduler._is_trading = False
    cmds = ['STOP']

    def get_cmd():
        if cmds:
            return cmds.pop(0)
        raise KeyboardInterrupt

    with patch('scheduler.db.get_brain_command', side_effect=get_cmd), \
         patch('scheduler.db.get_config', return_value=None), \
         patch('scheduler.db.end_session') as mock_end, \
         patch('scheduler.db.write_config'), \
         patch('scheduler.db.update_heartbeat'), \
         patch('scheduler.token_refresher.maybe_daily_refresh'), \
         patch('scheduler.time.sleep'):
        try:
            scheduler.run()
        except (KeyboardInterrupt, StopIteration):
            pass

    mock_end.assert_not_called()


# --- resume rebuilds the circuit-breaker streak ---

def _closed(reason, pnl):
    return {'status': 'CLOSED', 'entry_price': 100, 'pnl': pnl,
            'exit_reason': reason}


def test_resume_stats_rebuilds_consecutive_loss_streak():
    brain = TradingBrain.__new__(TradingBrain)
    brain.session_stats = {'trades_executed': 0, 'total_pnl': 0.0,
                           'winning_trades': 0, 'losing_trades': 0}
    brain.consecutive_losses = 0
    # newest-first (matches db.get_session_trades ordering)
    trades = [
        _closed('STOP_LOSS_HIT', -50),
        _closed('STOP_LOSS_HIT', -40),
        _closed('EOD_CLOSE', 5),       # doesn't touch the streak
        _closed('TARGET_HIT', 80),     # streak boundary
        _closed('STOP_LOSS_HIT', -30),
    ]
    with patch('brain.db.get_session_trades', return_value=trades):
        brain.resume_stats('sess-x')
    assert brain.consecutive_losses == 2


def test_resume_stats_streak_zero_after_recent_target_hit():
    brain = TradingBrain.__new__(TradingBrain)
    brain.session_stats = {'trades_executed': 0, 'total_pnl': 0.0,
                           'winning_trades': 0, 'losing_trades': 0}
    brain.consecutive_losses = 99
    trades = [_closed('TARGET_HIT', 80), _closed('STOP_LOSS_HIT', -30)]
    with patch('brain.db.get_session_trades', return_value=trades):
        brain.resume_stats('sess-x')
    assert brain.consecutive_losses == 0


# --- zombie trade guards ---

def test_execute_sell_by_trade_skips_null_quantity():
    brain = TradingBrain.__new__(TradingBrain)
    brain.order_manager = MagicMock()
    brain.kite = MagicMock()
    brain.session_id = 'sess-x'
    trade = {'symbol': 'INFY', 'exchange': 'NSE', 'quantity': None}
    brain._execute_sell_by_trade(trade, 1500.0, 'STOP_LOSS_HIT')
    brain.order_manager.place_sell_order.assert_not_called()


def test_initialize_voids_unfilled_trades(monkeypatch):
    """initialize must call cleanup_unfilled_trades for the current session."""
    import brain as brain_mod
    called = {}
    monkeypatch.setattr(brain_mod.config, 'QA_MODE', True)
    monkeypatch.setattr(brain_mod.db, 'cleanup_stale_open_trades', lambda sid: 0)
    monkeypatch.setattr(brain_mod.db, 'cleanup_unfilled_trades',
                        lambda sid: called.setdefault('sid', sid))
    monkeypatch.setattr(brain_mod.db, 'add_holdings_to_universe', lambda h: None)
    monkeypatch.setattr(brain_mod.db, 'get_win_rate', lambda: (0.45, 0))
    b = TradingBrain()
    b.initialize('tok', {'sessionId': 'sess-z', 'capitalDeployedd': 0,
                         'stockUniverse': 'HOLDINGS'})
    assert called.get('sid') == 'sess-z'


# --- unrealized P&L feeds the loss limit conservatively ---

def _stats(total_pnl, unrealized):
    return {'total_pnl': total_pnl, 'trades_executed': 1,
            'consecutive_losses': 0, 'unrealized_pnl': unrealized,
            'winning_trades': 0, 'losing_trades': 1}


_CFG = {'capitalDeployed': 10000, 'maxLossPercent': 5,
        'maxProfitPercent': 15, 'maxTrades': 25}


def test_open_losses_count_against_loss_limit():
    rm = RiskManager()
    with patch.object(rm, 'is_market_open', return_value=True):
        # realized -300 alone is inside the -500 limit; open -300 breaches it
        res = rm.check_session_limits(_stats(-300, -300), _CFG)
    assert res['can_trade'] is False


def test_open_gains_do_not_buy_loss_headroom():
    rm = RiskManager()
    with patch.object(rm, 'is_market_open', return_value=True):
        # realized -600 breaches the limit; +1000 unrealized must not save it
        res = rm.check_session_limits(_stats(-600, 1000), _CFG)
    assert res['can_trade'] is False


def test_open_gains_do_not_trigger_profit_target():
    rm = RiskManager()
    with patch.object(rm, 'is_market_open', return_value=True):
        # profit target 1500 realized-only: +2000 unrealized alone ≠ stop
        res = rm.check_session_limits(_stats(100, 2000), _CFG)
    assert res['can_trade'] is True


def test_unrealized_pnl_math_long_and_short():
    brain = TradingBrain.__new__(TradingBrain)
    brain.session_id = 'sess-x'
    brain.market_data = MagicMock()
    brain.market_data._holdings_cache = {
        'NSE:INFY': {'price': 90},
        'NSE:TCS': {'price': 110},
    }
    trades = [
        {'symbol': 'INFY', 'exchange': 'NSE', 'quantity': 10,
         'entry_value': 1000, 'position_type': 'LONG'},   # now 900 → -100
        {'symbol': 'TCS', 'exchange': 'NSE', 'quantity': 10,
         'entry_value': 1000, 'position_type': 'SHORT'},  # now 1100 → -100
    ]
    with patch('brain.db.get_open_trades', return_value=trades):
        assert brain._unrealized_pnl() == pytest.approx(-200)


# --- startup interlocks ---

def test_assert_safe_boot_blocks_qa_mode_on_prod_db(monkeypatch):
    import config
    monkeypatch.setattr(config, 'QA_MODE', True)
    monkeypatch.setattr(config, 'SUPABASE_URL',
                        'https://gilmuwmtdpjccibfhqtx.supabase.co')
    monkeypatch.setattr(config, 'PAPER_TRADING', True)
    with pytest.raises(RuntimeError, match='PRODUCTION'):
        config.assert_safe_boot()


def test_assert_safe_boot_blocks_real_trading_without_confirm(monkeypatch):
    import config
    monkeypatch.setattr(config, 'QA_MODE', False)
    monkeypatch.setattr(config, 'PAPER_TRADING', False)
    monkeypatch.delenv('REAL_TRADING_CONFIRM', raising=False)
    with pytest.raises(RuntimeError, match='PAPER_TRADING'):
        config.assert_safe_boot()


def test_assert_safe_boot_allows_paper_run(monkeypatch):
    import config
    monkeypatch.setattr(config, 'QA_MODE', False)
    monkeypatch.setattr(config, 'PAPER_TRADING', True)
    config.assert_safe_boot()  # must not raise
