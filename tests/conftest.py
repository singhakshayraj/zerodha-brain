import pytest
import random
from unittest.mock import MagicMock, patch
from datetime import datetime, timezone
import sys
import os

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


@pytest.fixture
def capital():
    return 10000.0


@pytest.fixture
def sample_candles_15min():
    """200 realistic 15min candles, seeded for reproducibility."""
    random.seed(42)
    candles, price = [], 1350.0
    for i in range(200):
        chg = random.uniform(-3.0, 3.0)
        o, c = price, price + chg
        h = max(o, c) + random.uniform(0, 1.5)
        l = min(o, c) - random.uniform(0, 1.5)
        candles.append({
            "open": round(o, 2), "high": round(h, 2),
            "low": round(l, 2), "close": round(c, 2),
            "volume": random.randint(5000, 200000),
            "timestamp": f"2026-05-21T{(i % 24):02d}:00:00+05:30"
        })
        price = c
    return candles


@pytest.fixture
def few_candles():
    """Only 5 candles — insufficient for most indicators."""
    return [
        {"open": 100, "high": 102, "low": 99, "close": 101,
         "volume": 1000, "timestamp": "2026-05-21T09:15:00+05:30"}
    ] * 5


@pytest.fixture
def mock_supabase():
    """Mock Supabase client — prevents any real DB calls."""
    with patch("database.supabase") as mock:
        mock.table.return_value.select.return_value \
            .eq.return_value.execute.return_value \
            .data = []
        mock.table.return_value.upsert.return_value \
            .execute.return_value.data = [{"id": "test-id"}]
        mock.table.return_value.insert.return_value \
            .execute.return_value.data = [{"id": "test-id"}]
        mock.table.return_value.update.return_value \
            .eq.return_value.execute.return_value.data = [{}]
        yield mock


@pytest.fixture
def sample_signal_trending():
    """A valid BUY signal in TRENDING regime."""
    return {
        "action": "BUY",
        "confidence": 75,
        "regime": "TRENDING",
        "stop_loss": 1320.0,
        "target": 1400.0,
        "risk_reward_ratio": 2.08,
        "indicators": {
            "rsi_14": 45.0, "atr_14": 12.5,
            "ema_21": 1340.0, "macd_histogram": 0.5
        }
    }


@pytest.fixture
def sample_signal_sell():
    """A valid SELL signal for SHORT."""
    return {
        "action": "SELL",
        "confidence": 80,
        "regime": "TRENDING",
        "stop_loss": 1380.0,
        "target": 1300.0,
        "risk_reward_ratio": 2.08,
        "indicators": {
            "rsi_14": 72.0, "atr_14": 12.5,
            "ema_21": 1360.0, "macd_histogram": -0.5
        }
    }
