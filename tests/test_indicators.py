"""T1.1 — Indicators unit tests."""
import pytest
from indicators import (
    get_closes, get_volumes, calculate_rsi, calculate_ema,
    calculate_ema_series, calculate_macd, calculate_bollinger_bands,
    calculate_atr, calculate_volume_sma, calculate_vwap,
    calculate_adx, get_candle_direction, run_all_indicators,
)


# --- helpers ---

def make_candles(closes, base_high_offset=1.0, base_low_offset=1.0, volume=10000):
    """Build minimal candle list from close values."""
    candles = []
    for i, c in enumerate(closes):
        o = closes[i - 1] if i > 0 else c
        candles.append({
            "open": o,
            "high": c + base_high_offset,
            "low": c - base_low_offset,
            "close": c,
            "volume": volume,
            "timestamp": f"2026-05-21T{(i % 24):02d}:00:00+05:30",
        })
    return candles


# --- get_closes / get_volumes ---

def test_get_closes_normal():
    candles = [{"close": 100}, {"close": 101}, {"close": 102}]
    assert get_closes(candles) == [100.0, 101.0, 102.0]


def test_get_closes_skips_none():
    candles = [{"close": 100}, {"close": None}, {"close": 102}]
    result = get_closes(candles)
    assert result == [100.0, 102.0]


def test_get_closes_empty():
    assert get_closes([]) == []


def test_get_volumes_normal():
    candles = [{"volume": 5000}, {"volume": 10000}]
    assert get_volumes(candles) == [5000.0, 10000.0]


def test_get_volumes_none_volume():
    candles = [{"volume": None}]
    assert get_volumes(candles) == [0.0]


# --- calculate_rsi ---

def test_rsi_insufficient_candles():
    candles = make_candles(list(range(10)))
    assert calculate_rsi(candles, 14) is None


def test_rsi_normal(sample_candles_15min):
    result = calculate_rsi(sample_candles_15min, 14)
    assert result is not None
    assert 0 <= result <= 100


def test_rsi_avg_loss_zero_returns_100():
    """All-up closes → avg_loss=0 → RSI=100."""
    closes = [100 + i for i in range(20)]
    candles = make_candles(closes)
    result = calculate_rsi(candles, 14)
    assert result == 100.0


def test_rsi_exception_returns_none():
    assert calculate_rsi(None, 14) is None


# --- calculate_ema_series ---

def test_ema_series_insufficient():
    result = calculate_ema_series([1.0, 2.0], 5)
    assert result == []


def test_ema_series_normal():
    values = [float(i) for i in range(1, 30)]
    series = calculate_ema_series(values, 9)
    assert len(series) > 0
    assert isinstance(series[-1], float)


def test_ema_returns_none_insufficient():
    result = calculate_ema([1.0, 2.0], 9)
    assert result is None


# --- calculate_macd ---

def test_macd_insufficient_returns_none():
    candles = make_candles(list(range(10)))
    assert calculate_macd(candles) is None


def test_macd_normal(sample_candles_15min):
    result = calculate_macd(sample_candles_15min)
    assert result is not None
    assert 'macd' in result
    assert 'signal' in result
    assert 'histogram' in result


def test_macd_exception_returns_none():
    assert calculate_macd(None) is None


# --- calculate_bollinger_bands ---

def test_bollinger_insufficient():
    candles = make_candles(list(range(5)))
    assert calculate_bollinger_bands(candles, period=20) is None


def test_bollinger_normal(sample_candles_15min):
    result = calculate_bollinger_bands(sample_candles_15min)
    assert result is not None
    assert result['upper'] > result['middle'] > result['lower']


# --- calculate_atr ---

def test_atr_insufficient_returns_none():
    candles = make_candles(list(range(5)))
    assert calculate_atr(candles, period=14) is None


def test_atr_normal(sample_candles_15min):
    result = calculate_atr(sample_candles_15min, 14)
    assert result is not None
    assert result > 0


# --- calculate_volume_sma ---

def test_volume_sma_insufficient():
    candles = make_candles([100] * 5)
    assert calculate_volume_sma(candles, period=20) is None


def test_volume_sma_normal(sample_candles_15min):
    result = calculate_volume_sma(sample_candles_15min, 20)
    assert result is not None and result > 0


# --- get_candle_direction ---

def test_candle_direction_bullish():
    candles = [
        {"open": 100, "close": 102},
        {"open": 102, "close": 104},
        {"open": 104, "close": 106},
    ]
    assert get_candle_direction(candles, 3) == 'BULLISH'


def test_candle_direction_bearish():
    candles = [
        {"open": 106, "close": 104},
        {"open": 104, "close": 102},
        {"open": 102, "close": 100},
    ]
    assert get_candle_direction(candles, 3) == 'BEARISH'


def test_candle_direction_neutral_mixed():
    candles = [
        {"open": 100, "close": 102},
        {"open": 102, "close": 101},
        {"open": 101, "close": 103},
    ]
    # 2 bullish → BULLISH (2 of 3 bullish)
    assert get_candle_direction(candles, 3) == 'BULLISH'


def test_candle_direction_insufficient():
    candles = [{"open": 100, "close": 102}]
    assert get_candle_direction(candles, 3) == 'NEUTRAL'


def test_candle_direction_empty():
    assert get_candle_direction([], 3) == 'NEUTRAL'


# --- run_all_indicators trend_strength branches ---

def test_run_all_indicators_returns_trend_strength(sample_candles_15min):
    result = run_all_indicators(sample_candles_15min)
    assert 'trend_strength' in result
    assert result['trend_strength'] in ('STRONG', 'WEAK', 'CHOPPY')


def test_run_all_indicators_choppy_when_no_adx():
    """Fewer than 29 candles → ADX returns None → trend_strength=CHOPPY."""
    candles = make_candles([100.0 + i * 0.1 for i in range(20)])
    result = run_all_indicators(candles)
    assert result['trend_strength'] == 'CHOPPY'


def test_run_all_indicators_candle_count(sample_candles_15min):
    result = run_all_indicators(sample_candles_15min)
    assert result['candle_count'] == len(sample_candles_15min)
