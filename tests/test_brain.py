"""T2.2 — TradingBrain unit tests. All external deps mocked."""
import pytest
from unittest.mock import MagicMock, patch
from contextlib import ExitStack
import os

# Patch env + supabase before any import chain reaches database.py
with patch.dict(os.environ, {
    'SUPABASE_URL': 'https://fake.supabase.co',
    'SUPABASE_SERVICE_KEY': 'fake-key',
}):
    with patch('supabase.create_client', return_value=MagicMock()):
        import database  # force load with mock client

import config
from kite_client import TokenExpiredError


# ---- helpers ----------------------------------------------------------------

def _make_brain():
    """Build a TradingBrain with all external deps mocked in."""
    from brain import TradingBrain
    brain = TradingBrain()

    brain.session_config = {
        'capitalDeployed': 10000.0,
        'maxTrades': 10,
        'maxLossPercent': 5.0,
        'maxProfitPercent': 15.0,
        'tradeIntervalSeconds': 300,
        'stockUniverse': 'NIFTY50',
        'sessionId': 'sess-test-001',
    }
    brain.session_id = 'sess-test-001'
    brain.kite = MagicMock()
    brain.market_data = MagicMock()
    brain.market_data._holdings_cache = {}
    brain.market_data._instrument_cache = {}
    brain.market_data.get_nifty_level.return_value = {
        'level': 22000.0, 'change_percent': 0.1, 'direction': 'NEUTRAL'
    }
    brain.market_data.get_candles.return_value = []
    brain.market_data.verify_instrument_tokens.return_value = []
    brain.market_data.get_live_quotes_batch.return_value = {}
    brain.market_data.get_fresh_close.return_value = None
    brain.market_data.refresh_holdings_cache.return_value = True
    brain.market_data.get_live_price_for_nifty50.return_value = 1350.0
    brain.signal_engine = MagicMock()
    brain.risk_manager = MagicMock()
    brain.order_manager = MagicMock()
    brain.universe = {}
    return brain


# Base set of db/logger patches shared by most run_cycle tests
_BASE_PATCHES = [
    ('brain.db.log_brain_activity', MagicMock()),
    ('brain.db.update_session', MagicMock()),
    ('brain.db.end_session', MagicMock()),
    ('brain.db.write_config', MagicMock()),
    ('brain.db.get_open_trades', MagicMock(return_value=[])),
    ('brain.db.get_win_rate', MagicMock(return_value=(0.5, 5))),
    ('brain.db.update_heartbeat', MagicMock()),
    ('brain.db.get_stock_universe', MagicMock(return_value=[])),
    ('brain.db.log_decision', MagicMock()),
    ('brain.db.close_trade', MagicMock()),
    ('brain.db.update_trade_entry', MagicMock()),
    ('brain.db.update_stock_score', MagicMock()),
    ('brain.logger.cycle', MagicMock()),
    ('brain.logger.set_context', MagicMock()),
    ('brain.logger.signal', MagicMock()),
    ('brain.logger.info', MagicMock()),
    ('brain.logger.error', MagicMock()),
    ('brain.logger.clear_context', MagicMock()),
    ('brain.logger.trade', MagicMock()),
    ('brain.time.sleep', MagicMock()),
]


def _stack(extra_patches=None):
    """Return an ExitStack with all base patches + optional extras applied."""
    stack = ExitStack()
    patches = dict(_BASE_PATCHES)
    if extra_patches:
        patches.update(extra_patches)
    for target, mock in patches.items():
        stack.enter_context(patch(target, mock))
    return stack


# ---- run_cycle tests --------------------------------------------------------

def test_run_cycle_token_expired_calls_end_session():
    brain = _make_brain()
    brain.market_data.refresh_holdings_cache.side_effect = TokenExpiredError("expired")

    with _stack():
        brain.run_cycle()

    assert brain._session_ended is True


def test_run_cycle_session_limit_reached_ends_session():
    brain = _make_brain()
    brain.risk_manager.check_session_limits.return_value = {
        'can_trade': False, 'reason': 'MAX_LOSS_HIT: exceeded'
    }
    brain.risk_manager.get_time_bucket.return_value = 'MORNING'

    extra = {
        'brain.TradingPrinciples.is_tradeable_indian_stock': MagicMock(
            return_value={'tradeable': True, 'reason': 'OK'}),
    }

    with _stack(extra):
        with patch.object(brain, '_auto_cover_shorts_if_eod'):
            with patch.object(brain, '_auto_close_longs_if_eod'):
                with patch.object(brain, '_check_and_close_positions'):
                    with patch.object(brain, '_is_past_ist', return_value=False):
                        brain.run_cycle()

    assert brain._session_ended is True


def test_run_cycle_per_cycle_limit_breaks_loop():
    brain = _make_brain()
    brain.risk_manager.check_session_limits.return_value = {'can_trade': True, 'reason': 'OK'}
    brain.risk_manager.get_time_bucket.return_value = 'MORNING'

    for i in range(4):
        sym = f'STOCK{i}'
        brain.universe[f'NSE:{sym}'] = {
            'symbol': sym, 'exchange': 'NSE',
            'instrument_token': 1000 + i, 'source': 'nifty50'
        }
        brain.market_data._holdings_cache[f'NSE:{sym}'] = {'price': 1000.0}

    executed_symbols = []
    brain.market_data.get_candles.return_value = [{'close': 1000}] * 50
    brain.signal_engine.generate_signal.return_value = {
        'action': 'BUY', 'confidence': 75, 'regime': 'TRENDING',
        'stop_loss': 980.0, 'target': 1040.0, 'risk_reward_ratio': 2.0,
        'indicators': {}, 'market_bias': 'NEUTRAL', 'skip_reasons': [], 'reasons': []
    }

    extra = {
        'brain.TradingPrinciples.is_tradeable_indian_stock': MagicMock(
            return_value={'tradeable': True, 'reason': 'OK'}),
    }

    with _stack(extra):
        with patch.object(brain, '_auto_cover_shorts_if_eod'):
            with patch.object(brain, '_auto_close_longs_if_eod'):
                with patch.object(brain, '_check_and_close_positions'):
                    with patch.object(brain, '_is_past_ist', return_value=False):
                        with patch.object(brain, '_execute_buy',
                                          side_effect=lambda s, e, p, sig: executed_symbols.append(s)):
                            with patch.object(brain, '_maybe_log_market_context'):
                                brain.run_cycle()

    assert len(executed_symbols) <= config.MAX_TRADES_PER_CYCLE


def test_run_cycle_buy_with_existing_short_covers():
    brain = _make_brain()
    brain.risk_manager.check_session_limits.return_value = {'can_trade': True, 'reason': 'OK'}
    brain.risk_manager.get_time_bucket.return_value = 'MORNING'
    brain.universe['NSE:RELIANCE'] = {
        'symbol': 'RELIANCE', 'exchange': 'NSE', 'instrument_token': 738561, 'source': 'nifty50'
    }
    brain.market_data._holdings_cache['NSE:RELIANCE'] = {'price': 2500.0}
    brain.market_data.get_candles.return_value = [{'close': 2500}] * 50

    short_trade = {
        'id': 'trade-short-1', 'symbol': 'RELIANCE', 'exchange': 'NSE',
        'position_type': 'SHORT', 'quantity': 2, 'entry_value': 5000.0,
        'stop_loss_price': 2550.0, 'target_price': 2450.0,
    }
    brain.signal_engine.generate_signal.return_value = {
        'action': 'BUY', 'confidence': 75, 'regime': 'TRENDING',
        'stop_loss': 2450.0, 'target': 2600.0, 'risk_reward_ratio': 2.0,
        'indicators': {}, 'market_bias': 'NEUTRAL', 'skip_reasons': [], 'reasons': []
    }

    cover_called = []
    extra = {
        'brain.db.get_open_trades': MagicMock(return_value=[short_trade]),
        'brain.TradingPrinciples.is_tradeable_indian_stock': MagicMock(
            return_value={'tradeable': True, 'reason': 'OK'}),
    }

    with _stack(extra):
        with patch.object(brain, '_auto_cover_shorts_if_eod'):
            with patch.object(brain, '_auto_close_longs_if_eod'):
                with patch.object(brain, '_check_and_close_positions'):
                    with patch.object(brain, '_is_past_ist', return_value=False):
                        with patch.object(brain, '_cover_short',
                                          side_effect=lambda t, p: cover_called.append(t['id'])):
                            with patch.object(brain, '_maybe_log_market_context'):
                                brain.run_cycle()

    assert 'trade-short-1' in cover_called


def test_run_cycle_buy_with_existing_long_skips():
    brain = _make_brain()
    brain.risk_manager.check_session_limits.return_value = {'can_trade': True, 'reason': 'OK'}
    brain.risk_manager.get_time_bucket.return_value = 'MORNING'
    brain.universe['NSE:TCS'] = {
        'symbol': 'TCS', 'exchange': 'NSE', 'instrument_token': 2953217, 'source': 'nifty50'
    }
    brain.market_data._holdings_cache['NSE:TCS'] = {'price': 3500.0}
    brain.market_data.get_candles.return_value = [{'close': 3500}] * 50

    long_trade = {'id': 'tl1', 'symbol': 'TCS', 'exchange': 'NSE', 'position_type': 'LONG', 'quantity': 1}
    brain.signal_engine.generate_signal.return_value = {
        'action': 'BUY', 'confidence': 75, 'regime': 'TRENDING',
        'stop_loss': 3400.0, 'target': 3700.0, 'risk_reward_ratio': 2.0,
        'indicators': {}, 'market_bias': 'NEUTRAL', 'skip_reasons': [], 'reasons': []
    }

    buy_called = []
    extra = {
        'brain.db.get_open_trades': MagicMock(return_value=[long_trade]),
        'brain.TradingPrinciples.is_tradeable_indian_stock': MagicMock(
            return_value={'tradeable': True, 'reason': 'OK'}),
    }

    with _stack(extra):
        with patch.object(brain, '_auto_cover_shorts_if_eod'):
            with patch.object(brain, '_auto_close_longs_if_eod'):
                with patch.object(brain, '_check_and_close_positions'):
                    with patch.object(brain, '_is_past_ist', return_value=False):
                        with patch.object(brain, '_execute_buy',
                                          side_effect=lambda *a, **k: buy_called.append(a[0])):
                            with patch.object(brain, '_maybe_log_market_context'):
                                brain.run_cycle()

    assert len(buy_called) == 0


def test_run_cycle_buy_no_existing_trade_executes_buy():
    brain = _make_brain()
    brain.risk_manager.check_session_limits.return_value = {'can_trade': True, 'reason': 'OK'}
    brain.risk_manager.get_time_bucket.return_value = 'MORNING'
    brain.universe['NSE:WIPRO'] = {
        'symbol': 'WIPRO', 'exchange': 'NSE', 'instrument_token': 969473, 'source': 'nifty50'
    }
    brain.market_data._holdings_cache['NSE:WIPRO'] = {'price': 450.0}
    brain.market_data.get_candles.return_value = [{'close': 450}] * 50
    brain.signal_engine.generate_signal.return_value = {
        'action': 'BUY', 'confidence': 75, 'regime': 'TRENDING',
        'stop_loss': 435.0, 'target': 480.0, 'risk_reward_ratio': 2.0,
        'indicators': {}, 'market_bias': 'NEUTRAL', 'skip_reasons': [], 'reasons': []
    }

    buy_called = []
    extra = {
        'brain.TradingPrinciples.is_tradeable_indian_stock': MagicMock(
            return_value={'tradeable': True, 'reason': 'OK'}),
    }

    with _stack(extra):
        with patch.object(brain, '_auto_cover_shorts_if_eod'):
            with patch.object(brain, '_auto_close_longs_if_eod'):
                with patch.object(brain, '_check_and_close_positions'):
                    with patch.object(brain, '_is_past_ist', return_value=False):
                        with patch.object(brain, '_execute_buy',
                                          side_effect=lambda *a, **k: buy_called.append(a[0])):
                            with patch.object(brain, '_maybe_log_market_context'):
                                brain.run_cycle()

    assert 'WIPRO' in buy_called


def test_run_cycle_sell_cnc_holding_blocked():
    brain = _make_brain()
    brain.risk_manager.check_session_limits.return_value = {'can_trade': True, 'reason': 'OK'}
    brain.risk_manager.get_time_bucket.return_value = 'MORNING'
    brain.universe['NSE:HDFCBANK'] = {
        'symbol': 'HDFCBANK', 'exchange': 'NSE', 'instrument_token': 341249,
        'source': 'holdings'  # CNC
    }
    brain.market_data._holdings_cache['NSE:HDFCBANK'] = {'price': 1600.0}
    brain.market_data.get_candles.return_value = [{'close': 1600}] * 50
    brain.signal_engine.generate_signal.return_value = {
        'action': 'SELL', 'confidence': 72, 'regime': 'TRENDING',
        'stop_loss': 1640.0, 'target': 1540.0, 'risk_reward_ratio': 2.0,
        'indicators': {}, 'market_bias': 'BEARISH', 'skip_reasons': [], 'reasons': []
    }

    short_called = []
    extra = {
        'brain.TradingPrinciples.is_tradeable_indian_stock': MagicMock(
            return_value={'tradeable': True, 'reason': 'OK'}),
    }

    with _stack(extra):
        with patch.object(brain, '_auto_cover_shorts_if_eod'):
            with patch.object(brain, '_auto_close_longs_if_eod'):
                with patch.object(brain, '_check_and_close_positions'):
                    with patch.object(brain, '_is_past_ist', return_value=False):
                        with patch.object(brain, '_open_short',
                                          side_effect=lambda *a, **k: short_called.append(a[0])):
                            with patch.object(brain, '_maybe_log_market_context'):
                                brain.run_cycle()

    assert len(short_called) == 0


def test_run_cycle_sell_noncnc_opens_short():
    brain = _make_brain()
    brain.risk_manager.check_session_limits.return_value = {'can_trade': True, 'reason': 'OK'}
    brain.risk_manager.get_time_bucket.return_value = 'MORNING'
    brain.universe['NSE:ICICIBANK'] = {
        'symbol': 'ICICIBANK', 'exchange': 'NSE', 'instrument_token': 1270529,
        'source': 'nifty50'
    }
    brain.market_data._holdings_cache['NSE:ICICIBANK'] = {'price': 900.0}
    brain.market_data.get_candles.return_value = [{'close': 900}] * 50
    brain.signal_engine.generate_signal.return_value = {
        'action': 'SELL', 'confidence': 70, 'regime': 'TRENDING',
        'stop_loss': 920.0, 'target': 860.0, 'risk_reward_ratio': 2.0,
        'indicators': {}, 'market_bias': 'BEARISH', 'skip_reasons': [], 'reasons': []
    }

    short_called = []
    extra = {
        'brain.TradingPrinciples.is_tradeable_indian_stock': MagicMock(
            return_value={'tradeable': True, 'reason': 'OK'}),
    }

    with _stack(extra):
        with patch.object(brain, '_auto_cover_shorts_if_eod'):
            with patch.object(brain, '_auto_close_longs_if_eod'):
                with patch.object(brain, '_check_and_close_positions'):
                    with patch.object(brain, '_is_past_ist', return_value=False):
                        with patch.object(brain, '_open_short',
                                          side_effect=lambda *a, **k: short_called.append(a[0])):
                            with patch.object(brain, '_maybe_log_market_context'):
                                brain.run_cycle()

    assert 'ICICIBANK' in short_called


# ---- _execute_buy tests -----------------------------------------------------

def test_execute_buy_qty_zero_no_order():
    brain = _make_brain()
    brain.risk_manager.calculate_position_size.return_value = 0
    signal = {'confidence': 75, 'stop_loss': 480.0, 'target': 530.0,
               'risk_reward_ratio': 2.0, 'regime': 'TRENDING', 'indicators': {}}

    with patch('brain.db.create_trade', return_value={'id': 't1'}):
        with patch('brain.db.get_win_rate', return_value=(0.5, 5)):
            with patch('brain.db.log_brain_activity'):
                with patch('brain.db.get_stock_universe', return_value=[]):
                    brain._execute_buy('INFY', 'NSE', 500.0, signal)

    brain.order_manager.place_buy_order.assert_not_called()


def test_execute_buy_price_deviation_warning(capsys):
    brain = _make_brain()
    brain.risk_manager.calculate_position_size.return_value = 5
    brain.order_manager.place_buy_order.return_value = {
        'order_id': 'ORD-DEV', 'status': 'COMPLETE',
        'price': 600.0,  # 20% above 500 → triggers warning
        'quantity': 5, 'value': 3000.0,
    }
    signal = {'confidence': 75, 'stop_loss': 480.0, 'target': 530.0,
               'risk_reward_ratio': 2.0, 'regime': 'TRENDING', 'indicators': {}}

    with patch('brain.db.create_trade', return_value={'id': 't1'}):
        with patch('brain.db.get_win_rate', return_value=(0.5, 5)):
            with patch('brain.db.update_trade_entry'):
                with patch('brain.db.log_brain_activity'):
                    with patch('brain.db.update_stock_score'):
                        with patch('brain.logger.trade'):
                            with patch('brain.db.get_stock_universe', return_value=[]):
                                brain._execute_buy('INFY', 'NSE', 500.0, signal)

    captured = capsys.readouterr()
    assert 'WARNING' in captured.out or 'mismatch' in captured.out


def test_execute_buy_order_fails_closes_trade():
    brain = _make_brain()
    brain.risk_manager.calculate_position_size.return_value = 5
    brain.order_manager.place_buy_order.return_value = None
    signal = {'confidence': 75, 'stop_loss': 480.0, 'target': 530.0,
               'risk_reward_ratio': 2.0, 'regime': 'TRENDING', 'indicators': {}}

    with patch('brain.db.create_trade', return_value={'id': 'trade-fail'}):
        with patch('brain.db.get_win_rate', return_value=(0.5, 5)):
            with patch('brain.db.close_trade') as mock_close:
                with patch('brain.db.log_brain_activity'):
                    with patch('brain.db.get_stock_universe', return_value=[]):
                        brain._execute_buy('SBIN', 'NSE', 500.0, signal)

    mock_close.assert_called_once()
    assert mock_close.call_args[0][1]['exit_reason'] == 'ORDER_FAILED'


def test_execute_buy_links_decision_to_trade():
    """The originating decision must be stamped with the trade it produced —
    the (features -> outcome) join for supervised learning."""
    brain = _make_brain()
    brain.risk_manager.calculate_position_size.return_value = 5
    brain.order_manager.place_buy_order.return_value = {
        'order_id': 'o1', 'price': 500.0, 'value': 2500.0, 'quantity': 5}
    signal = {'confidence': 75, 'stop_loss': 480.0, 'target': 530.0,
              'risk_reward_ratio': 2.0, 'regime': 'TRENDING', 'indicators': {}}
    with patch('brain.db.create_trade', return_value={'id': 'trade-99'}), \
         patch('brain.db.get_win_rate', return_value=(0.5, 5)), \
         patch('brain.db.update_trade_entry'), \
         patch('brain.db.log_brain_activity'), \
         patch('brain.db.get_stock_universe', return_value=[]), \
         patch('brain.db.link_decision_trade') as link:
        brain._execute_buy('SBIN', 'NSE', 500.0, signal, decision_id='dec-42')
    link.assert_called_once_with('dec-42', 'trade-99')


# ---- _open_short tests -------------------------------------------------------

def test_open_short_qty_zero_no_order():
    brain = _make_brain()
    brain.risk_manager.calculate_position_size.return_value = 0
    signal = {'confidence': 70, 'stop_loss': 480.0, 'target': 520.0,
               'risk_reward_ratio': 2.0, 'regime': 'TRENDING', 'indicators': {}}

    with patch('brain.db.create_trade', return_value={'id': 't1'}):
        with patch('brain.db.get_win_rate', return_value=(0.5, 5)):
            with patch('brain.db.log_brain_activity'):
                with patch('brain.db.get_stock_universe', return_value=[]):
                    brain._open_short('WIPRO', 'NSE', 500.0, signal)

    brain.order_manager.place_short_order.assert_not_called()


def test_open_short_stop_inversion():
    """short_stop = price + (price - long_stop) → must be above price."""
    brain = _make_brain()
    brain.risk_manager.calculate_position_size.return_value = 3
    brain.order_manager.place_short_order.return_value = None
    signal = {
        'confidence': 70,
        'stop_loss': 480.0,   # 20 below 500
        'target': 540.0,
        'risk_reward_ratio': 2.0,
        'regime': 'TRENDING',
        'indicators': {},
    }

    captured_sl = []

    def fake_create(sess_id, data):
        captured_sl.append(data.get('stop_loss_price'))
        return {'id': 'short-t'}

    with patch('brain.db.create_trade', side_effect=fake_create):
        with patch('brain.db.get_win_rate', return_value=(0.5, 5)):
            with patch('brain.db.close_trade'):
                with patch('brain.db.log_brain_activity'):
                    with patch('brain.db.get_stock_universe', return_value=[]):
                        brain._open_short('WIPRO', 'NSE', 500.0, signal)

    assert len(captured_sl) == 1
    assert captured_sl[0] == 520.0  # 500 + (500 - 480) = 520


def test_open_short_links_decision_to_trade():
    brain = _make_brain()
    brain.risk_manager.calculate_position_size.return_value = 3
    brain.order_manager.place_short_order.return_value = {
        'order_id': 'o1', 'price': 500.0, 'value': 1500.0, 'quantity': 3}
    signal = {'confidence': 70, 'stop_loss': 480.0, 'target': 540.0,
              'risk_reward_ratio': 2.0, 'regime': 'TRENDING', 'indicators': {}}
    with patch('brain.db.create_trade', return_value={'id': 'short-77'}), \
         patch('brain.db.get_win_rate', return_value=(0.5, 5)), \
         patch('brain.db.update_trade_entry'), \
         patch('brain.db.log_brain_activity'), \
         patch('brain.db.get_stock_universe', return_value=[]), \
         patch('brain.db.link_decision_trade') as link:
        brain._open_short('WIPRO', 'NSE', 500.0, signal, decision_id='dec-7')
    link.assert_called_once_with('dec-7', 'short-77')


# ---- _check_and_close_positions tests ---------------------------------------

def test_check_and_close_long_stop_hit():
    brain = _make_brain()
    trade = {
        'id': 'long-1', 'symbol': 'RELIANCE', 'exchange': 'NSE',
        'position_type': 'LONG', 'quantity': 2,
        'stop_loss_price': 2400.0, 'target_price': 2600.0, 'entry_value': 5000.0,
    }
    brain.market_data.get_fresh_close.return_value = 2390.0
    sell_called = []

    with patch('brain.db.get_open_trades', return_value=[trade]):
        with patch('brain.db.end_session'):
            with patch('brain.db.write_config'):
                with patch('brain.db.update_heartbeat'):
                    with patch.object(brain, '_execute_sell_by_trade',
                                      side_effect=lambda t, p, r: sell_called.append(r)):
                        brain._check_and_close_positions()

    assert 'STOP_LOSS_HIT' in sell_called


def test_check_and_close_long_target_hit():
    brain = _make_brain()
    trade = {
        'id': 'long-2', 'symbol': 'TCS', 'exchange': 'NSE',
        'position_type': 'LONG', 'quantity': 1,
        'stop_loss_price': 3300.0, 'target_price': 3700.0, 'entry_value': 3500.0,
    }
    brain.market_data.get_fresh_close.return_value = 3750.0
    sell_called = []

    with patch('brain.db.get_open_trades', return_value=[trade]):
        with patch('brain.db.end_session'):
            with patch('brain.db.write_config'):
                with patch('brain.db.update_heartbeat'):
                    with patch.object(brain, '_execute_sell_by_trade',
                                      side_effect=lambda t, p, r: sell_called.append(r)):
                        brain._check_and_close_positions()

    assert 'TARGET_HIT' in sell_called


def test_check_and_close_short_stop_hit():
    brain = _make_brain()
    trade = {
        'id': 'short-1', 'symbol': 'INFY', 'exchange': 'NSE',
        'position_type': 'SHORT', 'quantity': 3,
        'stop_loss_price': 1500.0, 'target_price': 1400.0, 'entry_value': 4350.0,
    }
    brain.market_data.get_fresh_close.return_value = 1510.0
    cover_called = []

    with patch('brain.db.get_open_trades', return_value=[trade]):
        with patch('brain.db.end_session'):
            with patch('brain.db.write_config'):
                with patch('brain.db.update_heartbeat'):
                    with patch.object(brain, '_cover_short',
                                      side_effect=lambda t, p: cover_called.append('STOP')):
                        brain._check_and_close_positions()

    assert 'STOP' in cover_called


def test_check_and_close_short_target_hit():
    brain = _make_brain()
    trade = {
        'id': 'short-2', 'symbol': 'WIPRO', 'exchange': 'NSE',
        'position_type': 'SHORT', 'quantity': 5,
        'stop_loss_price': 470.0, 'target_price': 430.0, 'entry_value': 2250.0,
    }
    brain.market_data.get_fresh_close.return_value = 425.0
    cover_called = []

    with patch('brain.db.get_open_trades', return_value=[trade]):
        with patch('brain.db.end_session'):
            with patch('brain.db.write_config'):
                with patch('brain.db.update_heartbeat'):
                    with patch.object(brain, '_cover_short',
                                      side_effect=lambda t, p: cover_called.append('TARGET')):
                        brain._check_and_close_positions()

    assert 'TARGET' in cover_called


def test_check_and_close_circuit_breaker():
    brain = _make_brain()
    brain.consecutive_losses = 2
    trade = {
        'id': 'cb-1', 'symbol': 'SBIN', 'exchange': 'NSE',
        'position_type': 'LONG', 'quantity': 4,
        'stop_loss_price': 490.0, 'target_price': 550.0, 'entry_value': 2000.0,
    }
    brain.market_data.get_fresh_close.return_value = 480.0
    end_called = []

    # The streak is now owned by _record_close_outcome, fired from the close
    # path — so the mocked sell must record the realized loss like the real
    # one would, otherwise the counter never advances.
    def _sell(t, price, reason):
        brain._record_close_outcome(t['symbol'], -25.0)

    with patch('brain.db.get_open_trades', return_value=[trade]):
        with patch('brain.db.end_session'):
            with patch('brain.db.write_config'):
                with patch('brain.db.update_heartbeat'):
                    with patch('brain.db.log_brain_activity'):
                        with patch.object(brain, '_execute_sell_by_trade',
                                          side_effect=_sell):
                            with patch.object(brain, 'end_session',
                                              side_effect=lambda r: end_called.append(r)):
                                brain._check_and_close_positions()

    assert any('CIRCUIT_BREAKER' in r for r in end_called)


# ---- end_session ------------------------------------------------------------

def test_end_session_squares_off_all_positions():
    brain = _make_brain()
    open_trades = [
        {'id': 't1', 'symbol': 'RELIANCE', 'exchange': 'NSE',
         'position_type': 'LONG', 'quantity': 2, 'entry_value': 5000.0},
        {'id': 't2', 'symbol': 'INFY', 'exchange': 'NSE',
         'position_type': 'SHORT', 'quantity': 3, 'entry_value': 4200.0},
    ]
    brain.market_data._holdings_cache = {
        'NSE:RELIANCE': {'price': 2510.0},
        'NSE:INFY': {'price': 1450.0},
    }

    sell_calls = []
    cover_calls = []

    with patch('brain.db.end_session'):
        with patch('brain.db.write_config'):
            with patch('brain.db.log_brain_activity'):
                with patch('brain.db.update_heartbeat'):
                    with patch('brain.logger.clear_context'):
                        with patch('brain.logger.info'):
                            with patch('brain.db.get_open_trades') as mock_open:
                                mock_open.side_effect = [open_trades, [], []]
                                with patch.object(brain, '_execute_sell_by_trade',
                                                  side_effect=lambda t, p, r: sell_calls.append(t['id'])):
                                    with patch.object(brain, '_cover_short',
                                                      side_effect=lambda t, p: cover_calls.append(t['id'])):
                                        brain.end_session('MANUAL_STOP')

    assert 't1' in sell_calls
    assert 't2' in cover_calls


# ---- resume_stats (brain-restart mid-session) --------------------------------

def test_resume_stats_rebuilds_from_closed_trades():
    brain = _make_brain()
    trades = [
        {'status': 'CLOSED', 'entry_price': 100, 'pnl': 50.0},
        {'status': 'CLOSED', 'entry_price': 100, 'pnl': -20.0},
        {'status': 'OPEN', 'entry_price': 100, 'pnl': None},
        {'status': 'CANCELLED', 'entry_price': None, 'pnl': None},
    ]
    with patch('brain.db.get_session_trades', return_value=trades) as mock_get:
        brain.resume_stats('sess-test-001')

    mock_get.assert_called_once_with('sess-test-001')
    assert brain.session_stats['trades_executed'] == 3  # all with entry_price set
    assert brain.session_stats['total_pnl'] == 30.0
    assert brain.session_stats['winning_trades'] == 1
    assert brain.session_stats['losing_trades'] == 1


def test_resume_stats_empty_session_stays_zeroed():
    brain = _make_brain()
    with patch('brain.db.get_session_trades', return_value=[]):
        brain.resume_stats('sess-test-001')

    assert brain.session_stats['trades_executed'] == 0
    assert brain.session_stats['total_pnl'] == 0.0


# ---- initialize -------------------------------------------------------------

def test_initialize_removes_bad_token():
    from brain import TradingBrain
    brain = TradingBrain()
    session_config = {
        'capitalDeployed': 10000.0,
        'maxTrades': 10,
        'maxLossPercent': 5.0,
        'maxProfitPercent': 15.0,
        'tradeIntervalSeconds': 300,
        'stockUniverse': 'NIFTY50',
        'sessionId': 'sess-init-test',
    }

    mock_kite = MagicMock()
    mock_kite.get_holdings.return_value = []
    mock_kite.get_instruments.side_effect = Exception("unavailable")

    mock_md = MagicMock()
    mock_md._holdings_cache = {}
    mock_md._instrument_cache = {}
    mock_md.verify_instrument_tokens.return_value = [
        ('NSE:RELIANCE', 738561, 1500.0, 2500.0)
    ]
    mock_md.refresh_holdings_cache.return_value = True

    with patch('brain.KiteClient', return_value=mock_kite):
        with patch('brain.MarketData', return_value=mock_md):
            with patch('brain.db.cleanup_stale_open_trades'):
                with patch('brain.db.add_holdings_to_universe'):
                    with patch('brain.db.get_win_rate', return_value=(0.5, 5)):
                        with patch('brain.db.write_config'):
                            with patch('brain.logger.set_context'):
                                with patch('brain.logger.info'):
                                    result = brain.initialize('fake-token', session_config)

    assert result is True
    assert 'NSE:RELIANCE' not in brain.universe


def _init_with_universe(stock_universe):
    """Run brain.initialize() with a given stockUniverse and return the brain."""
    from brain import TradingBrain
    brain = TradingBrain()
    session_config = {
        'capitalDeployed': 10000.0,
        'maxTrades': 10,
        'maxLossPercent': 5.0,
        'maxProfitPercent': 15.0,
        'tradeIntervalSeconds': 300,
        'stockUniverse': stock_universe,
        'sessionId': 'sess-init-test',
    }

    mock_kite = MagicMock()
    mock_kite.get_holdings.return_value = []
    mock_kite.get_instruments.side_effect = Exception("unavailable")

    mock_md = MagicMock()
    mock_md._holdings_cache = {}
    mock_md._instrument_cache = {}
    mock_md.verify_instrument_tokens.return_value = []
    mock_md.refresh_holdings_cache.return_value = True

    with patch('brain.KiteClient', return_value=mock_kite):
        with patch('brain.MarketData', return_value=mock_md):
            with patch('brain.db.cleanup_stale_open_trades'):
                with patch('brain.db.add_holdings_to_universe'):
                    with patch('brain.db.get_win_rate', return_value=(0.5, 5)):
                        with patch('brain.db.write_config'):
                            with patch('brain.logger.set_context'):
                                with patch('brain.logger.info'):
                                    brain.initialize('fake-token', session_config)
    return brain, mock_md


def test_initialize_nifty50_excludes_next50_stocks():
    brain, mock_md = _init_with_universe('NIFTY50')
    assert 'NSE:RELIANCE' in brain.universe          # NIFTY50
    assert 'NSE:DMART' not in brain.universe          # NIFTY Next 50 only
    assert brain.universe['NSE:RELIANCE']['source'] == 'nifty50'
    # verify_instrument_tokens is called with only what was actually added
    called_map = mock_md.verify_instrument_tokens.call_args[0][0]
    assert 'NSE:RELIANCE' in called_map
    assert 'NSE:DMART' not in called_map


def test_initialize_both_includes_next50_stocks():
    brain, mock_md = _init_with_universe('BOTH')
    assert 'NSE:RELIANCE' in brain.universe           # NIFTY50
    assert 'NSE:DMART' in brain.universe              # NIFTY Next 50
    assert brain.universe['NSE:DMART']['source'] == 'nifty_next50'
    called_map = mock_md.verify_instrument_tokens.call_args[0][0]
    assert 'NSE:RELIANCE' in called_map
    assert 'NSE:DMART' in called_map


def test_initialize_holdings_only_adds_no_index_stocks():
    brain, mock_md = _init_with_universe('HOLDINGS')
    assert 'NSE:RELIANCE' not in brain.universe
    assert 'NSE:DMART' not in brain.universe
    called_map = mock_md.verify_instrument_tokens.call_args[0][0]
    assert called_map == {}
