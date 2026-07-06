"""Watchdog check logic + error-budget tests (Tier 1 alerting)."""
import os
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytz

with patch.dict(os.environ, {
    'SUPABASE_URL': 'https://fake.supabase.co',
    'SUPABASE_SERVICE_KEY': 'fake-key',
}):
    with patch('supabase.create_client', return_value=MagicMock()):
        import database  # noqa

import watchdog

IST = pytz.timezone('Asia/Kolkata')


def _ist(y, mo, d, h, mi):
    return IST.localize(datetime(y, mo, d, h, mi))


def _fresh_heartbeat(status='RUNNING', message='ok', age_seconds=10):
    ping = datetime.now(timezone.utc) - timedelta(seconds=age_seconds)
    return {'last_ping': ping.isoformat(), 'status': status, 'message': message}


# A known trading Tuesday, mid-market (2026-07-07 is not an NSE holiday)
TRADING_MIDDAY = _ist(2026, 7, 7, 12, 0)


def _state(**over):
    s = {
        'heartbeat': _fresh_heartbeat(),
        'brain_status': 'RUNNING',
        'active_session_id': 'sess-1',
        'trades_today': 3,
    }
    s.update(over)
    return s


def _keys(alerts):
    return [k for _, k, _ in alerts]


# --- calendar gating ---

def test_no_alerts_on_weekend():
    saturday = _ist(2026, 7, 11, 12, 0)
    assert watchdog.evaluate(_state(heartbeat=None), saturday) == []


def test_no_market_alerts_before_open():
    early = _ist(2026, 7, 7, 8, 0)
    assert watchdog.evaluate(_state(heartbeat=None), early) == []


# --- token reminder ---

def test_token_reminder_fires_in_morning_window():
    morning = _ist(2026, 7, 7, 8, 50)
    keys = _keys(watchdog.evaluate(_state(), morning))
    assert keys == ['token-reminder-2026-07-07']


def test_no_token_reminder_midday():
    assert 'token-reminder-2026-07-07' not in _keys(
        watchdog.evaluate(_state(), TRADING_MIDDAY))


# --- heartbeat checks ---

def test_healthy_state_no_alerts():
    assert watchdog.evaluate(_state(), TRADING_MIDDAY) == []


def test_stale_heartbeat_is_critical():
    st = _state(heartbeat=_fresh_heartbeat(age_seconds=600))
    assert 'heartbeat-stale' in _keys(watchdog.evaluate(st, TRADING_MIDDAY))


def test_missing_heartbeat_is_critical():
    st = _state(heartbeat=None)
    assert 'heartbeat-missing' in _keys(watchdog.evaluate(st, TRADING_MIDDAY))


def test_degraded_heartbeat_alerts():
    st = _state(heartbeat=_fresh_heartbeat(status='DEGRADED', message='db errors'))
    assert 'heartbeat-degraded' in _keys(watchdog.evaluate(st, TRADING_MIDDAY))


def test_error_heartbeat_alerts():
    st = _state(heartbeat=_fresh_heartbeat(status='ERROR', message='boom'))
    assert 'heartbeat-error' in _keys(watchdog.evaluate(st, TRADING_MIDDAY))


# --- token expiry + zero trades ---

def test_token_expired_alerts():
    st = _state(brain_status='TOKEN_EXPIRED')
    assert 'token-expired' in _keys(watchdog.evaluate(st, TRADING_MIDDAY))


def test_zero_trades_by_11_alerts():
    st = _state(trades_today=0)
    assert 'zero-trades-2026-07-07' in _keys(
        watchdog.evaluate(st, TRADING_MIDDAY))


def test_zero_trades_before_11_no_alert():
    st = _state(trades_today=0)
    at_10 = _ist(2026, 7, 7, 10, 0)
    assert 'zero-trades-2026-07-07' not in _keys(watchdog.evaluate(st, at_10))


def test_zero_trades_without_session_no_alert():
    st = _state(trades_today=0, active_session_id=None)
    assert watchdog.evaluate(st, TRADING_MIDDAY) == []


def test_trades_query_failed_skips_check():
    st = _state(trades_today=None)
    assert watchdog.evaluate(st, TRADING_MIDDAY) == []


# --- alert dedup ---

def test_send_alert_dedups_within_window():
    watchdog._last_sent.clear()
    assert watchdog.send_alert('k1', 'msg', now_ts=1000.0, tier=watchdog.P3) is True
    assert watchdog.send_alert('k1', 'msg', now_ts=1100.0, tier=watchdog.P3) is False
    assert watchdog.send_alert(
        'k1', 'msg', now_ts=1000.0 + watchdog.ALERT_REPEAT_SECONDS + 1,
        tier=watchdog.P3) is True


# --- error budget → DEGRADED ---

def test_health_degraded_after_threshold_and_resets():
    database._record_success()
    assert database.health_degraded() is False
    for _ in range(database.DEGRADED_THRESHOLD):
        database._record_failure()
    assert database.health_degraded() is True
    database._record_success()
    assert database.health_degraded() is False


def test_heartbeat_thread_reports_degraded():
    import scheduler
    import threading

    scheduler._set_heartbeat('RUNNING', 3, 'Cycle 3')
    calls = []
    done = threading.Event()

    def fake_update(s, c, m):
        calls.append((s, c, m))
        done.set()

    def fake_sleep(n):
        if done.is_set():
            raise KeyboardInterrupt

    with patch('scheduler.db.update_heartbeat', side_effect=fake_update), \
         patch('scheduler.db.health_degraded', return_value=True), \
         patch('scheduler.time.sleep', side_effect=fake_sleep):
        t = threading.Thread(target=scheduler._heartbeat_thread, daemon=True)
        t.start()
        done.wait(timeout=3)
        t.join(timeout=1)

    assert calls and calls[0][0] == 'DEGRADED'
