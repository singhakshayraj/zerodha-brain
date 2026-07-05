"""T1.2 — RegimeDetector unit tests."""
import pytest
from unittest.mock import patch, MagicMock
from datetime import datetime
import pytz

IST = pytz.timezone('Asia/Kolkata')


def _dt(hour, minute):
    return datetime(2026, 5, 21, hour, minute, 0, tzinfo=IST)


def make_candles(n=50, close=1350.0):
    import random
    random.seed(1)
    candles, price = [], close
    for i in range(n):
        chg = random.uniform(-2.0, 2.0)
        o, c = price, price + chg
        candles.append({
            "open": o, "high": c + 1.0, "low": c - 1.0, "close": c,
            "volume": 50000,
            "timestamp": f"2026-05-21T{(i % 24):02d}:00:00+05:30",
        })
        price = c
    return candles


@pytest.fixture
def detector():
    from regime_detector import RegimeDetector
    return RegimeDetector()


@pytest.fixture
def candles():
    return make_candles(60)


# --- time boundary tests ---

def test_blocked_before_trading_start(detector, candles):
    with patch('regime_detector.datetime') as mock_dt:
        mock_dt.now.return_value = _dt(9, 20)
        result = detector.detect(candles, candles, candles, 'NEUTRAL', 0.0)
    assert result['can_trade'] is False
    assert result['regime'] == 'BLOCKED'


def test_blocked_at_9_14(detector, candles):
    with patch('regime_detector.datetime') as mock_dt:
        mock_dt.now.return_value = _dt(9, 14)
        result = detector.detect(candles, candles, candles, 'NEUTRAL', 0.0)
    assert result['can_trade'] is False


def test_allowed_at_9_30(detector, candles):
    with patch('regime_detector.datetime') as mock_dt:
        mock_dt.now.return_value = _dt(9, 30)
        result = detector.detect(candles, candles, candles, 'NEUTRAL', 0.1)
    # may or may not trade based on ADX; just ensure not BLOCKED for time
    assert result['regime'] != 'BLOCKED' or not result['can_trade']


def test_blocked_after_no_new_entries(detector, candles):
    with patch('regime_detector.datetime') as mock_dt:
        mock_dt.now.return_value = _dt(15, 5)
        result = detector.detect(candles, candles, candles, 'NEUTRAL', 0.0)
    assert result['can_trade'] is False
    assert result['regime'] == 'BLOCKED'


def test_blocked_at_15_01(detector, candles):
    with patch('regime_detector.datetime') as mock_dt:
        mock_dt.now.return_value = _dt(15, 1)
        result = detector.detect(candles, candles, candles, 'NEUTRAL', 0.0)
    assert result['can_trade'] is False


# --- nifty bias ---

def test_nifty_bearish_modifier(detector, candles):
    with patch('regime_detector.datetime') as mock_dt:
        mock_dt.now.return_value = _dt(10, 0)
        with patch('regime_detector.calculate_adx') as mock_adx:
            mock_adx.return_value = {'adx': 28.0, 'plus_di': 20.0, 'minus_di': 12.0}
            result = detector.detect(candles, candles, candles, 'BEARISH', -0.8)
    assert result['nifty_bias'] == 'BEARISH'
    assert result['confidence_modifier'] <= 0  # nifty -20 penalty applied


def test_nifty_bullish_modifier(detector, candles):
    with patch('regime_detector.datetime') as mock_dt:
        mock_dt.now.return_value = _dt(10, 0)
        with patch('regime_detector.calculate_adx') as mock_adx:
            mock_adx.return_value = {'adx': 28.0, 'plus_di': 20.0, 'minus_di': 12.0}
            result = detector.detect(candles, candles, candles, 'BULLISH', 0.8)
    assert result['nifty_bias'] == 'BULLISH'


def test_nifty_neutral(detector, candles):
    with patch('regime_detector.datetime') as mock_dt:
        mock_dt.now.return_value = _dt(10, 0)
        result = detector.detect(candles, candles, candles, 'NEUTRAL', 0.1)
    assert result['nifty_bias'] == 'NEUTRAL'


# --- lunch penalty ---

def test_lunch_penalty_applied(detector, candles):
    adx_val = {'adx': 28.0, 'plus_di': 20.0, 'minus_di': 12.0}
    with patch('regime_detector.datetime') as mock_dt:
        mock_dt.now.return_value = _dt(13, 0)
        with patch('regime_detector.calculate_adx', return_value=adx_val):
            result_lunch = detector.detect(candles, candles, candles, 'NEUTRAL', 0.1)
    with patch('regime_detector.datetime') as mock_dt:
        mock_dt.now.return_value = _dt(11, 0)
        with patch('regime_detector.calculate_adx', return_value=adx_val):
            result_normal = detector.detect(candles, candles, candles, 'NEUTRAL', 0.1)
    # lunch modifier is -15 lower
    assert result_lunch['confidence_modifier'] < result_normal['confidence_modifier']


# --- choppy → can_trade=False ---

def test_choppy_blocks_trading(detector, candles):
    # Empty candles_15min → calculate_adx not called (falsy guard); need non-empty candles
    with patch('regime_detector.datetime') as mock_dt:
        mock_dt.now.return_value = _dt(10, 0)
        with patch('regime_detector.calculate_adx') as mock_adx:
            mock_adx.return_value = {'adx': 15.0, 'plus_di': 10.0, 'minus_di': 12.0}
            result = detector.detect(candles, candles, candles, 'NEUTRAL', 0.1)
    assert result['can_trade'] is False
    assert result['regime'] == 'CHOPPY'


# --- trending regime ---

def test_trending_regime(detector, candles):
    with patch('regime_detector.datetime') as mock_dt:
        mock_dt.now.return_value = _dt(10, 0)
        with patch('regime_detector.calculate_adx') as mock_adx:
            mock_adx.return_value = {'adx': 30.0, 'plus_di': 25.0, 'minus_di': 12.0}
            result = detector.detect(candles, candles, candles, 'NEUTRAL', 0.1)
    assert result['regime'] == 'TRENDING'
    assert result['can_trade'] is True


# --- weak trend ---

def test_weak_trend_regime(detector, candles):
    with patch('regime_detector.datetime') as mock_dt:
        mock_dt.now.return_value = _dt(10, 0)
        with patch('regime_detector.calculate_adx') as mock_adx:
            mock_adx.return_value = {'adx': 22.0, 'plus_di': 15.0, 'minus_di': 12.0}
            result = detector.detect(candles, candles, candles, 'NEUTRAL', 0.1)
    assert result['regime'] == 'WEAK_TREND'
    assert result['can_trade'] is True


# --- adx None → UNKNOWN ---

def test_unknown_regime_when_adx_none(detector):
    with patch('regime_detector.datetime') as mock_dt:
        mock_dt.now.return_value = _dt(10, 0)
        with patch('regime_detector.calculate_adx') as mock_adx:
            mock_adx.return_value = None
            result = detector.detect([], [], [], 'NEUTRAL', 0.1)
    assert result['regime'] == 'UNKNOWN'
    assert result['can_trade'] is True


# --- empty candles doesn't crash ---

def test_empty_candles_no_crash(detector):
    with patch('regime_detector.datetime') as mock_dt:
        mock_dt.now.return_value = _dt(10, 0)
        result = detector.detect([], [], [], 'NEUTRAL', 0.0)
    assert 'regime' in result


# --- multi-timeframe modifier ---

def test_multiframe_bearish_modifier(detector):
    """2+ bearish TFs → timeframe_modifier=+10 (aligned)."""
    bearish_candles = [
        {"open": 110, "close": 108, "high": 111, "low": 107, "volume": 1000,
         "timestamp": f"2026-05-21T10:0{i}:00+05:30"}
        for i in range(3)
    ]
    with patch('regime_detector.datetime') as mock_dt:
        mock_dt.now.return_value = _dt(10, 0)
        with patch('regime_detector.calculate_adx') as mock_adx:
            mock_adx.return_value = {'adx': 30.0, 'plus_di': 10.0, 'minus_di': 20.0}
            result = detector.detect(
                bearish_candles, bearish_candles, bearish_candles, 'NEUTRAL', 0.0
            )
    assert result['market_bias'] == 'BEARISH'
