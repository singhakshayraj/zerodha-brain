"""REQ-051 time-stop + REQ-053 event-day calendar tests."""
import os
from datetime import date, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytz

with patch.dict(os.environ, {
    'SUPABASE_URL': 'https://fake.supabase.co',
    'SUPABASE_SERVICE_KEY': 'fake-key',
}):
    with patch('supabase.create_client', return_value=MagicMock()):
        import database  # noqa

import config
import event_calendar as ec
from brain import TradingBrain

IST = pytz.timezone('Asia/Kolkata')


# --- event calendar: expiry math ---

def test_weekly_expiry_is_tuesday():
    assert ec.is_weekly_expiry(date(2026, 7, 7)) is True    # Tuesday
    assert ec.is_weekly_expiry(date(2026, 7, 8)) is False   # Wednesday


def test_monthly_expiry_is_last_tuesday():
    # July 2026 Tuesdays: 7, 14, 21, 28 → last is 28
    assert ec.is_monthly_expiry(date(2026, 7, 28)) is True
    assert ec.is_monthly_expiry(date(2026, 7, 21)) is False
    assert ec.is_monthly_expiry(date(2026, 7, 27)) is False  # Monday


def test_policy_normal_on_nonevent_day():
    p = ec.policy(date(2026, 7, 8), 'RELIANCE')  # Wednesday
    assert p['policy'] == ec.NORMAL


def test_policy_raise_bar_weekly_expiry_heavyweight():
    p = ec.policy(date(2026, 7, 7), 'RELIANCE')  # Tuesday, heavyweight
    assert p['policy'] == ec.RAISE_BAR
    assert p['weekly_expiry'] is True and p['monthly_expiry'] is False


def test_policy_stand_aside_monthly_expiry_heavyweight():
    p = ec.policy(date(2026, 7, 28), 'INFY')     # last Tuesday, heavyweight
    assert p['policy'] == ec.STAND_ASIDE
    assert p['monthly_expiry'] is True


def test_policy_normal_for_nonheavyweight_on_expiry():
    p = ec.policy(date(2026, 7, 7), 'SOMESMALLCAP')
    assert p['policy'] == ec.NORMAL


def test_policy_results_day_stands_aside_any_day():
    p = ec.policy(date(2026, 7, 8), 'TCS', results_symbols=['TCS'])
    assert p['policy'] == ec.STAND_ASIDE
    assert 'RESULTS_DAY' in p['reasons']


def test_policy_accepts_iso_string():
    assert ec.policy('2026-07-07T09:15:00+0530', 'RELIANCE')['policy'] == ec.RAISE_BAR


# --- time-stop: _minutes_open ---

def _brain():
    b = TradingBrain.__new__(TradingBrain)
    return b


def test_minutes_open_computes_age():
    b = _brain()
    entry = (datetime.now(IST) - timedelta(minutes=42)).isoformat()
    mins = b._minutes_open({'entry_time': entry})
    assert 41 <= mins <= 44


def test_minutes_open_none_without_entry_time():
    assert _brain()._minutes_open({}) is None
    assert _brain()._minutes_open({'entry_time': 'garbage'}) is None


def test_minutes_open_handles_naive_timestamp():
    b = _brain()
    naive = (datetime.now(IST).replace(tzinfo=None) - timedelta(minutes=30)).isoformat()
    mins = b._minutes_open({'entry_time': naive})
    assert mins is not None and 28 <= mins <= 32


# --- time-stop firing (gated) ---

def _brain_with_one_open(entry_minutes_ago, position_type='LONG'):
    b = _brain()
    b.session_id = 's1'
    b.consecutive_losses = 0
    b._session_ended = False
    b._time_stop_logged = set()
    b._excursion = {}
    entry = (datetime.now(IST) - timedelta(minutes=entry_minutes_ago)).isoformat()
    trade = {
        'id': 't1', 'symbol': 'INFY', 'exchange': 'NSE',
        'position_type': position_type, 'entry_time': entry,
        'stop_loss_price': 1, 'target_price': 10_000,   # never hit
        'quantity': 10, 'entry_value': 1000,
    }
    b.market_data = MagicMock()
    b.market_data.get_fresh_close.return_value = 100
    b.market_data._holdings_cache = {}
    return b, trade


def test_time_stop_fires_when_enabled_and_past_limit(monkeypatch):
    monkeypatch.setattr(config, 'TIME_STOP_ENABLED', True)
    monkeypatch.setattr(config, 'TIME_STOP_MIN', 40)
    b, trade = _brain_with_one_open(entry_minutes_ago=45)
    with patch('brain.db.get_open_trades', return_value=[trade]), \
         patch('brain.db.log_brain_activity'), \
         patch.object(b, '_execute_sell_by_trade') as sell, \
         patch.object(b, '_cover_short'):
        b._check_and_close_positions()
    sell.assert_called_once()
    assert sell.call_args[0][2] == 'TIME_STOP'


def test_time_stop_does_not_fire_when_disabled(monkeypatch):
    monkeypatch.setattr(config, 'TIME_STOP_ENABLED', False)
    monkeypatch.setattr(config, 'TIME_STOP_MIN', 40)
    b, trade = _brain_with_one_open(entry_minutes_ago=45)
    logged = []
    with patch('brain.db.get_open_trades', return_value=[trade]), \
         patch('brain.db.log_brain_activity',
               side_effect=lambda **k: logged.append(k)), \
         patch.object(b, '_execute_sell_by_trade') as sell, \
         patch.object(b, '_cover_short'):
        b._check_and_close_positions()
    sell.assert_not_called()
    # but it records that it WOULD have fired
    assert any(l.get('activity_type') == 'TIME_STOP_WOULD_FIRE' for l in logged)


def test_time_stop_not_past_limit_no_action(monkeypatch):
    monkeypatch.setattr(config, 'TIME_STOP_ENABLED', True)
    monkeypatch.setattr(config, 'TIME_STOP_MIN', 40)
    b, trade = _brain_with_one_open(entry_minutes_ago=20)
    with patch('brain.db.get_open_trades', return_value=[trade]), \
         patch('brain.db.log_brain_activity'), \
         patch.object(b, '_execute_sell_by_trade') as sell, \
         patch.object(b, '_cover_short'):
        b._check_and_close_positions()
    sell.assert_not_called()


def test_time_stop_shorts_use_tighter_limit(monkeypatch):
    monkeypatch.setattr(config, 'TIME_STOP_ENABLED', True)
    monkeypatch.setattr(config, 'TIME_STOP_MIN_SHORT', 25)
    b, trade = _brain_with_one_open(entry_minutes_ago=30, position_type='SHORT')
    # short stop is ABOVE entry; give a stop it won't hit at price 100
    trade['stop_loss_price'] = 10_000
    trade['target_price'] = 1
    with patch('brain.db.get_open_trades', return_value=[trade]), \
         patch('brain.db.log_brain_activity'), \
         patch.object(b, '_cover_short') as cover, \
         patch.object(b, '_execute_sell_by_trade'):
        b._check_and_close_positions()
    cover.assert_called_once()
