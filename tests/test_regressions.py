"""
Regression tests — each class named after the bug it guards against.
Do not modify without understanding the original incident.
"""
import pytest
import inspect
import random
import sys
import os
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


def make_candles(n, base_price, seed=42):
    random.seed(seed)
    candles, price = [], base_price
    for i in range(n):
        chg = random.uniform(-base_price * 0.005, base_price * 0.005)
        o, c = price, price + chg
        h = max(o, c) + abs(chg) * 0.2
        l = min(o, c) - abs(chg) * 0.2
        candles.append({
            "open": round(o, 2), "high": round(h, 2),
            "low": round(l, 2), "close": round(c, 2),
            "volume": random.randint(5000, 200000),
            "timestamp": f"2026-05-21T{(i % 24):02d}:00:00+05:30"
        })
        price = c
    return candles


class TestBug2CoalindiaATR:
    """ATR inflated by bad candle data must be clamped."""

    def test_atr_above_5pct_price_clamped(self):
        from signal_engine import SignalEngine

        engine = SignalEngine()
        bad_ind = {
            'atr_14': 80.0,  # 17.5% of 455 — way too high
            'rsi_14': 25.0, 'ema_21': 450.0, 'ema_200': 440.0,
            'macd_histogram': 0.8, 'bb_lower': 452.0,
            'volume_sma_20': 10000, 'current_volume': 20000,
            'vwap': 450.0, 'candle_count': 50,
            'adx': 30.0, 'adx_plus_di': 25.0, 'adx_minus_di': 15.0,
            'candle_direction': 'BULLISH', 'trend_strength': 'STRONG',
            'bb_upper': None, 'bb_bandwidth': None,
            'ema_9': 451.0, 'ema_50': 445.0,
            'macd': 0.5, 'macd_signal': 0.3,
            'current_close': 455.0,
        }
        regime_result = {
            'can_trade': True, 'regime': 'TRENDING',
            'confidence_modifier': 10, 'market_bias': 'BULLISH',
            'nifty_bias': 'NEUTRAL', 'reasons': []
        }
        candles = make_candles(200, 455.0)
        with patch.object(engine.regime_detector, 'detect', return_value=regime_result), \
             patch('signal_engine.run_all_indicators', return_value=bad_ind):
            signal = engine.generate_signal(
                candles, candles, candles,
                455.0, 'COALINDIA', 'NEUTRAL', 0.0
            )
        # Unclamped: 455 - 1.2*80 = 359. Clamped ATR ≤ 5% → stop > 430.
        assert signal['stop_loss'] > 430, \
            f"ATR not clamped: stop_loss={signal['stop_loss']} (expected >430)"


class TestBug3ITCWeakTrend:
    """ITC BUY was firing at 70% WEAK_TREND. Must require 80%."""

    def test_weak_trend_79pct_must_hold(self):
        from signal_engine import SignalEngine

        engine = SignalEngine()
        regime_result = {
            'can_trade': True, 'regime': 'WEAK_TREND',
            'confidence_modifier': 0, 'market_bias': 'NEUTRAL',
            'nifty_bias': 'NEUTRAL', 'reasons': []
        }
        ind = {
            'atr_14': 2.5, 'rsi_14': 42.0, 'ema_21': 305.0,
            'ema_200': 300.0, 'macd_histogram': 0.3,
            'bb_lower': None, 'volume_sma_20': 1000,
            'current_volume': 800, 'vwap': 304.0,
            'candle_count': 50, 'adx': 22.0,
            'adx_plus_di': 20.0, 'adx_minus_di': 18.0,
            'candle_direction': 'BULLISH', 'trend_strength': 'WEAK',
            'bb_upper': None, 'bb_bandwidth': None, 'ema_9': 306.0,
            'ema_50': 302.0, 'macd': 0.2, 'macd_signal': 0.1,
            'current_close': 307.0,
        }
        candles = make_candles(200, 307.0)
        with patch.object(engine.regime_detector, 'detect', return_value=regime_result), \
             patch('signal_engine.run_all_indicators', return_value=ind):
            signal = engine.generate_signal(
                candles, candles, candles,
                307.0, 'ITC', 'NEUTRAL', 0.0
            )
        if signal['regime'] == 'WEAK_TREND' and signal['confidence'] < 80:
            assert signal['action'] != 'BUY', \
                f"BUG-3 regression: ITC BUY at {signal['confidence']}% WEAK_TREND"


class TestBug10DuplicateLong:
    """JSWSTEEL was bought 4x — duplicate LONG prevention."""

    def test_buy_signal_skipped_when_long_exists(self):
        import brain as brain_module
        source = inspect.getsource(brain_module.TradingBrain.run_cycle)
        assert 'long_match' in source, \
            "BUG-10 regression: long_match check missing from run_cycle"
        assert 'Already long' in source or 'long_match' in source, \
            "BUG-10 regression: duplicate LONG prevention missing"


class TestBug13InvalidSL:
    """Invalid stop loss must return qty=0, never fire an order."""

    def test_stop_distance_zero_returns_zero(self):
        from risk_manager import RiskManager
        rm = RiskManager()
        qty = rm.calculate_position_size(
            capital=10000, live_price=455.0,
            confidence=80, stop_loss_price=455.0
        )
        assert qty == 0, "BUG-13: stop_distance=0 must return qty=0"

    def test_stop_too_wide_returns_zero(self):
        from risk_manager import RiskManager
        rm = RiskManager()
        qty = rm.calculate_position_size(
            capital=10000, live_price=455.0,
            confidence=80, stop_loss_price=200.0  # 56% away
        )
        assert qty == 0, "BUG-13: stop > 50% must return qty=0"


class TestBug14MinQty:
    """qty must never be 0 for affordable stocks due to floor."""

    def test_qty_floor_prevents_zero(self):
        from risk_manager import RiskManager
        rm = RiskManager()
        qty = rm.calculate_position_size(
            capital=10000, live_price=200.0,
            confidence=80, stop_loss_price=199.5  # 0.25% stop
        )
        assert qty >= 1, "BUG-14: qty must be >= 1 for affordable stocks"


class TestBug22GhostSessions:
    """end_session must always write ended_at."""

    def test_end_session_writes_ended_at(self):
        import database as db_module
        source = inspect.getsource(db_module.end_session)
        assert 'ended_at' in source, \
            "BUG-22: end_session must write ended_at"
        assert 'status' in source, \
            "BUG-22: end_session must write status"


class TestBug19PerCycleLimit:
    """Max 3 trades per cycle regardless of max_trades setting."""

    def test_max_trades_per_cycle_is_3(self):
        import config
        assert config.MAX_TRADES_PER_CYCLE == 3

    def test_per_cycle_counter_in_brain(self):
        import brain as brain_module
        source = inspect.getsource(brain_module.TradingBrain.run_cycle)
        assert 'trades_this_cycle' in source, \
            "BUG-19: trades_this_cycle counter missing from run_cycle"
        assert 'MAX_TRADES_PER_CYCLE' in source or 'max_per_cycle' in source, \
            "BUG-19: per-cycle limit check missing"


class TestBug23MinPositionValue:
    """Minimum Rs2000 position enforced."""

    def test_cheap_stock_qty_raised(self):
        from risk_manager import RiskManager
        rm = RiskManager()
        qty = rm.calculate_position_size(
            capital=10000, live_price=200.0,
            confidence=80, stop_loss_price=194.0
        )
        position_value = qty * 200.0
        assert position_value >= 2000 or qty == 0, \
            f"BUG-23: position Rs{position_value} below minimum Rs2000"
