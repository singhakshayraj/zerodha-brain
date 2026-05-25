import pytest
import time
import sys
import os
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))


@pytest.fixture
def mock_kite():
    return MagicMock()


@pytest.fixture
def market_data(mock_kite):
    from market_data import MarketData
    return MarketData(kite=mock_kite)


class TestRefreshHoldingsCache:
    def test_success_returns_true_and_populates_cache(self, market_data, mock_kite):
        mock_kite.get_holdings.return_value = [
            {
                'tradingsymbol': 'RELIANCE',
                'exchange': 'NSE',
                'last_price': 1350.0,
                'instrument_token': 738561,
                'close_price': 1340.0,
                'volume': 10000,
            }
        ]
        result = market_data.refresh_holdings_cache()
        assert result is True
        assert 'NSE:RELIANCE' in market_data._holdings_cache

    def test_exception_returns_false(self, market_data, mock_kite):
        mock_kite.get_holdings.side_effect = Exception("API error")
        result = market_data.refresh_holdings_cache()
        assert result is False


class TestGetCandles:
    def test_within_ttl_returns_cached_no_api(self, market_data, mock_kite):
        cache_key = 'NSE:RELIANCE_15minute'
        fake_candles = [{'close': 1350.0}]
        market_data._candle_cache[cache_key] = fake_candles
        market_data._candle_cache_time[cache_key] = time.time()  # just now
        market_data._instrument_cache['NSE:RELIANCE'] = 738561

        result = market_data.get_candles('NSE:RELIANCE', '15minute')
        assert result == fake_candles
        mock_kite._get.assert_not_called()

    def test_expired_cache_calls_get_historical(self, market_data, mock_kite):
        cache_key = 'NSE:RELIANCE_15minute'
        market_data._candle_cache[cache_key] = []
        market_data._candle_cache_time[cache_key] = 0  # expired
        market_data._instrument_cache['NSE:RELIANCE'] = 738561

        mock_kite._get.return_value = {'candles': [
            ['2026-05-21T09:15:00', 1340, 1360, 1335, 1350, 50000]
        ]}
        result = market_data.get_candles('NSE:RELIANCE', '15minute')
        mock_kite._get.assert_called_once()
        assert len(result) == 1

    def test_no_instrument_token_returns_empty(self, market_data, mock_kite):
        result = market_data.get_candles('NSE:UNKNOWN_XYZ', '15minute')
        assert result == []
        mock_kite._get.assert_not_called()


class TestGetHistorical:
    def test_5min_from_dt_is_3_days_back(self, market_data, mock_kite):
        mock_kite._get.return_value = {'candles': []}
        with patch('market_data.MarketData._now') as mock_now:
            from datetime import datetime
            import pytz
            IST = pytz.timezone('Asia/Kolkata')
            now = datetime(2026, 5, 21, 10, 0, 0, tzinfo=IST)
            mock_now.return_value = now
            market_data._get_historical(738561, '5minute', 3)

        call_args = mock_kite._get.call_args
        from_str = call_args[1]['params']['from']
        from_dt_str = from_str[:10]
        assert from_dt_str == '2026-05-18', f"Expected 3 days back, got {from_dt_str}"

    def test_15min_from_dt_is_5_days_back(self, market_data, mock_kite):
        mock_kite._get.return_value = {'candles': []}
        with patch('market_data.MarketData._now') as mock_now:
            from datetime import datetime
            import pytz
            IST = pytz.timezone('Asia/Kolkata')
            now = datetime(2026, 5, 21, 10, 0, 0, tzinfo=IST)
            mock_now.return_value = now
            market_data._get_historical(738561, '15minute', 5)

        call_args = mock_kite._get.call_args
        from_str = call_args[1]['params']['from']
        from_dt_str = from_str[:10]
        assert from_dt_str == '2026-05-16', f"Expected 5 days back, got {from_dt_str}"

    def test_api_error_returns_empty(self, market_data, mock_kite):
        mock_kite._get.side_effect = Exception("Network error")
        result = market_data._get_historical(738561, '15minute', 5)
        assert result == []


class TestIsBlockedSymbol:
    def test_nifty_index_is_blocked(self, market_data):
        assert market_data._is_blocked_symbol('NSE:NIFTY 50') is True

    def test_regular_stock_not_blocked(self, market_data):
        assert market_data._is_blocked_symbol('NSE:RELIANCE') is False
