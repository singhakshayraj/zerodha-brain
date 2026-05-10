from datetime import datetime

import numpy as np
import pytz

IST = pytz.timezone('Asia/Kolkata')


def get_closes(candles: list) -> list:
    try:
        return [float(c['close']) for c in candles if c.get('close') is not None]
    except Exception as e:
        print(f"[indicators.get_closes] error: {e}")
        return []


def get_volumes(candles: list) -> list:
    try:
        return [float(c.get('volume') or 0) for c in candles]
    except Exception as e:
        print(f"[indicators.get_volumes] error: {e}")
        return []


def calculate_rsi(candles: list, period: int = 14):
    try:
        closes = get_closes(candles)
        if len(closes) < period + 1:
            return None

        gains = []
        losses = []
        for i in range(1, len(closes)):
            ch = closes[i] - closes[i - 1]
            gains.append(max(ch, 0))
            losses.append(max(-ch, 0))

        if len(gains) < period:
            return None

        avg_gain = sum(gains[:period]) / period
        avg_loss = sum(losses[:period]) / period

        for i in range(period, len(gains)):
            avg_gain = (avg_gain * (period - 1) + gains[i]) / period
            avg_loss = (avg_loss * (period - 1) + losses[i]) / period

        if avg_loss == 0:
            return 100.0
        rs = avg_gain / avg_loss
        rsi = 100 - (100 / (1 + rs))
        return round(rsi, 2)
    except Exception as e:
        print(f"[indicators.calculate_rsi] error: {e}")
        return None


def calculate_ema(values: list, period: int):
    try:
        series = calculate_ema_series(values, period)
        if not series:
            return None
        return round(series[-1], 2)
    except Exception as e:
        print(f"[indicators.calculate_ema] error: {e}")
        return None


def calculate_ema_series(values: list, period: int) -> list:
    try:
        if len(values) < period:
            return []
        multiplier = 2 / (period + 1)
        seed = sum(values[:period]) / period
        series = [seed]
        for v in values[period:]:
            ema = (v - series[-1]) * multiplier + series[-1]
            series.append(ema)
        return series
    except Exception as e:
        print(f"[indicators.calculate_ema_series] error: {e}")
        return []


def calculate_macd(candles: list):
    try:
        closes = get_closes(candles)
        if len(closes) < 35:
            return None

        fast_series = calculate_ema_series(closes, 12)
        slow_series = calculate_ema_series(closes, 26)
        if not fast_series or not slow_series:
            return None

        # Align: fast started 11 idx in, slow at 25 idx in
        # We need MACD line aligned to slow series length
        offset = len(fast_series) - len(slow_series)
        fast_aligned = fast_series[offset:]
        macd_line = [f - s for f, s in zip(fast_aligned, slow_series)]

        signal_series = calculate_ema_series(macd_line, 9)
        if not signal_series:
            return None

        macd_val = macd_line[-1]
        signal_val = signal_series[-1]
        hist = macd_val - signal_val

        return {
            'macd': round(macd_val, 2),
            'signal': round(signal_val, 2),
            'histogram': round(hist, 2),
        }
    except Exception as e:
        print(f"[indicators.calculate_macd] error: {e}")
        return None


def calculate_bollinger_bands(candles: list, period: int = 20, std_dev: int = 2):
    try:
        closes = get_closes(candles)
        if len(closes) < period:
            return None
        window = np.array(closes[-period:], dtype=float)
        middle = float(window.mean())
        std = float(window.std(ddof=0))
        upper = middle + std_dev * std
        lower = middle - std_dev * std
        bandwidth = ((upper - lower) / middle * 100) if middle else 0.0
        return {
            'upper': round(upper, 2),
            'middle': round(middle, 2),
            'lower': round(lower, 2),
            'bandwidth': round(bandwidth, 2),
        }
    except Exception as e:
        print(f"[indicators.calculate_bollinger_bands] error: {e}")
        return None


def calculate_atr(candles: list, period: int = 14):
    try:
        if len(candles) < period + 1:
            return None
        trs = []
        for i in range(1, len(candles)):
            high = float(candles[i]['high'])
            low = float(candles[i]['low'])
            prev_close = float(candles[i - 1]['close'])
            tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
            trs.append(tr)

        if len(trs) < period:
            return None

        atr = sum(trs[:period]) / period
        for tr in trs[period:]:
            atr = (atr * (period - 1) + tr) / period
        return round(atr, 2)
    except Exception as e:
        print(f"[indicators.calculate_atr] error: {e}")
        return None


def calculate_volume_sma(candles: list, period: int = 20):
    try:
        vols = get_volumes(candles)
        if len(vols) < period:
            return None
        return float(sum(vols[-period:]) / period)
    except Exception as e:
        print(f"[indicators.calculate_volume_sma] error: {e}")
        return None


def _candle_date(c: dict):
    ts = c.get('timestamp')
    if not ts:
        return None
    try:
        if isinstance(ts, str):
            dt = datetime.fromisoformat(ts.replace('Z', '+00:00'))
        else:
            dt = ts
        if dt.tzinfo is None:
            dt = IST.localize(dt)
        else:
            dt = dt.astimezone(IST)
        return dt.date()
    except Exception:
        return None


def calculate_vwap(candles: list):
    try:
        today = datetime.now(IST).date()
        today_candles = [c for c in candles if _candle_date(c) == today]
        if not today_candles:
            return None

        num = 0.0
        den = 0.0
        for c in today_candles:
            tp = (float(c['high']) + float(c['low']) + float(c['close'])) / 3
            vol = float(c.get('volume') or 0)
            num += tp * vol
            den += vol
        if den == 0:
            return None
        return round(num / den, 2)
    except Exception as e:
        print(f"[indicators.calculate_vwap] error: {e}")
        return None


def run_all_indicators(candles: list) -> dict:
    closes = get_closes(candles)
    current_close = closes[-1] if closes else 0.0
    current_volume = float(candles[-1].get('volume') or 0) if candles else 0.0

    macd = calculate_macd(candles)
    bb = calculate_bollinger_bands(candles)

    return {
        'rsi_14': calculate_rsi(candles, 14),
        'ema_9': calculate_ema(closes, 9),
        'ema_21': calculate_ema(closes, 21),
        'ema_50': calculate_ema(closes, 50),
        'ema_200': calculate_ema(closes, 200),
        'macd': macd['macd'] if macd else None,
        'macd_signal': macd['signal'] if macd else None,
        'macd_histogram': macd['histogram'] if macd else None,
        'bb_upper': bb['upper'] if bb else None,
        'bb_middle': bb['middle'] if bb else None,
        'bb_lower': bb['lower'] if bb else None,
        'bb_bandwidth': bb['bandwidth'] if bb else None,
        'atr_14': calculate_atr(candles, 14),
        'volume_sma_20': calculate_volume_sma(candles, 20),
        'vwap': calculate_vwap(candles),
        'current_volume': current_volume,
        'current_close': current_close,
        'candle_count': len(candles),
    }
