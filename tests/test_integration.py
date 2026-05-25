"""
Integration tests — full pipelines with real modules, mocked external APIs.
"""
import pytest
from unittest.mock import patch, MagicMock
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


class TestSignalToSizePipeline:
    """Full: signal_engine → risk_manager → valid qty."""

    def test_buy_signal_produces_valid_qty(self, sample_candles_15min):
        from signal_engine import SignalEngine
        from risk_manager import RiskManager

        engine = SignalEngine()
        rm = RiskManager()

        with patch.object(engine.regime_detector, 'detect') as mock_detect:
            mock_detect.return_value = {
                'can_trade': True, 'regime': 'TRENDING',
                'confidence_modifier': 15, 'market_bias': 'BULLISH',
                'nifty_bias': 'BULLISH', 'reasons': []
            }
            signal = engine.generate_signal(
                sample_candles_15min, sample_candles_15min,
                sample_candles_15min, 1350.0, 'RELIANCE',
                'BULLISH', 0.8
            )

        if signal['action'] == 'BUY':
            qty = rm.calculate_position_size(
                capital=10000.0,
                live_price=1350.0,
                confidence=signal['confidence'],
                stop_loss_price=signal['stop_loss'],
                target_price=signal['target'],
            )
            assert qty >= 0, "qty must be non-negative"
            if qty > 0:
                assert qty * 1350.0 <= 10000.0, "position must not exceed capital"

    def test_sell_signal_produces_inverted_stops(self, sample_candles_15min):
        from signal_engine import SignalEngine

        engine = SignalEngine()

        with patch.object(engine.regime_detector, 'detect') as mock_detect:
            mock_detect.return_value = {
                'can_trade': True, 'regime': 'TRENDING',
                'confidence_modifier': 10, 'market_bias': 'BEARISH',
                'nifty_bias': 'BEARISH', 'reasons': []
            }
            signal = engine.generate_signal(
                sample_candles_15min, sample_candles_15min,
                sample_candles_15min, 1350.0, 'HINDUNILVR',
                'BEARISH', -0.8
            )

        if signal['action'] == 'SELL':
            assert signal['stop_loss'] > 1350.0, \
                "SHORT stop_loss must be above price"
            assert signal['target'] < 1350.0, \
                "SHORT target must be below price"


class TestRegimeToSignalPipeline:
    """RegimeDetector output correctly gates signal generation."""

    def test_choppy_regime_blocks_all_signals(self, sample_candles_15min):
        from signal_engine import SignalEngine

        engine = SignalEngine()

        with patch.object(engine.regime_detector, 'detect') as mock:
            mock.return_value = {
                'can_trade': False, 'regime': 'CHOPPY',
                'confidence_modifier': 0, 'market_bias': 'NEUTRAL',
                'nifty_bias': 'NEUTRAL',
                'reasons': ['ADX below threshold']
            }
            signal = engine.generate_signal(
                sample_candles_15min, sample_candles_15min,
                sample_candles_15min, 1350.0, 'INFY',
                'NEUTRAL', 0.0
            )

        assert signal['action'] == 'HOLD', \
            f"CHOPPY must produce HOLD, got {signal['action']}"


class TestSessionLimitsPipeline:
    """check_session_limits correctly gates trading."""

    def test_max_trades_reached_blocks_cycle(self):
        from risk_manager import RiskManager

        rm = RiskManager()
        session_stats = {
            'total_pnl': 50.0,
            'trades_executed': 10,
            'consecutive_losses': 0,
        }
        session_config = {
            'capitalDeployed': 10000.0,
            'maxLossPercent': 3.0,
            'maxProfitPercent': 5.0,
            'maxTrades': 10,
        }
        with patch.object(rm, 'is_market_open', return_value=True):
            result = rm.check_session_limits(session_stats, session_config)
        assert result['can_trade'] is False
        assert 'MAX_TRADES' in result.get('reason', '')

    def test_profit_target_reached_blocks_cycle(self):
        from risk_manager import RiskManager

        rm = RiskManager()
        session_stats = {
            'total_pnl': 520.0,  # > 5% of 10000
            'trades_executed': 3,
            'consecutive_losses': 0,
        }
        session_config = {
            'capitalDeployed': 10000.0,
            'maxLossPercent': 3.0,
            'maxProfitPercent': 5.0,
            'maxTrades': 10,
        }
        with patch.object(rm, 'is_market_open', return_value=True):
            result = rm.check_session_limits(session_stats, session_config)
        assert result['can_trade'] is False


class TestConfigSanityIntegration:
    """All modules load with sane config values."""

    def test_all_modules_import_cleanly(self):
        modules = [
            'config', 'indicators', 'regime_detector',
            'trading_principles', 'risk_manager', 'signal_engine',
            'logger',
        ]
        for mod in modules:
            imported = __import__(mod)
            assert imported is not None, f"{mod} failed to import"

    def test_brain_imports_with_mocked_db(self):
        with patch('database.supabase') as mock:
            mock.table.return_value.select.return_value \
                .eq.return_value.execute.return_value.data = []
            import brain
            assert brain is not None
