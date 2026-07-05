"""Unit tests for the scheduler autopilot gate and holiday calendar."""
import os
from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest
import pytz

with patch.dict(os.environ, {
    'SUPABASE_URL': 'https://fake.supabase.co',
    'SUPABASE_SERVICE_KEY': 'fake-key',
}):
    with patch('supabase.create_client', return_value=MagicMock()):
        import database

import config
import scheduler

IST = pytz.timezone('Asia/Kolkata')


def _ist(year, month, day, hour, minute):
    return IST.localize(datetime(year, month, day, hour, minute))


# 2026-07-06 is a Monday, not a holiday
TRADING_DAY = (2026, 7, 6)


@pytest.fixture
def autopilot_on(monkeypatch):
    monkeypatch.setattr(config, 'AUTOPILOT', True)


def _gate(now, has_session=False):
    with patch.object(scheduler, 'datetime') as dt:
        dt.now.return_value = now
        with patch.object(database, 'has_session_today', return_value=has_session):
            return scheduler._should_autostart()


class TestShouldAutostart:
    def test_disabled_by_default(self):
        assert config.AUTOPILOT is False or os.getenv('AUTOPILOT')
        with patch.object(config, 'AUTOPILOT', False):
            assert scheduler._should_autostart() is False

    def test_fires_in_window_on_trading_day(self, autopilot_on):
        assert _gate(_ist(*TRADING_DAY, 9, 30)) is True

    def test_blocked_before_0930(self, autopilot_on):
        assert _gate(_ist(*TRADING_DAY, 9, 29)) is False

    def test_blocked_after_close(self, autopilot_on):
        assert _gate(_ist(*TRADING_DAY, 15, 20)) is False

    def test_blocked_on_weekend(self, autopilot_on):
        # 2026-07-05 is a Sunday
        assert _gate(_ist(2026, 7, 5, 10, 0)) is False

    def test_blocked_on_2026_holiday(self, autopilot_on):
        # Gandhi Jayanti 2026 falls on a Friday
        assert _gate(_ist(2026, 10, 2, 10, 0)) is False

    def test_blocked_when_session_exists_today(self, autopilot_on):
        assert _gate(_ist(*TRADING_DAY, 10, 0), has_session=True) is False


class TestHolidayCalendar:
    def test_2026_list_present_and_merged(self):
        assert '2026-10-02' in config.NSE_HOLIDAYS_2026
        assert '2026-12-25' in config.NSE_HOLIDAYS
        assert '2025-12-25' in config.NSE_HOLIDAYS

    def test_2026_dates_are_weekdays(self):
        # A weekend "holiday" would signal a typo in the hardcoded list
        # (Nov 8 Muhurat Sunday is deliberately excluded).
        for d in config.NSE_HOLIDAYS_2026:
            assert datetime.strptime(d, '%Y-%m-%d').weekday() <= 4, d

    def test_risk_manager_uses_merged_list(self):
        from risk_manager import RiskManager
        rm = RiskManager()
        with patch('risk_manager.datetime') as dt:
            dt.now.return_value = _ist(2026, 10, 2, 10, 0)  # holiday, Friday
            assert rm.is_market_open() is False
