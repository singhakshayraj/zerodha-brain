"""_maybe_run_advisor gating (2026-07-14 rewrite): DB-backed once-per-day
official-run dedup (no more in-memory flag — see regression tests below),
the intraday lite-refresh interval gate, the weekday/holiday guard, and the
manual 'advisor_run_now' trigger that bypasses all of it for an on-demand
run."""
import os
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytz

with patch.dict(os.environ, {
    'SUPABASE_URL': 'https://fake.supabase.co',
    'SUPABASE_SERVICE_KEY': 'fake-key',
}):
    with patch('supabase.create_client', return_value=MagicMock()):
        import database  # noqa

import config
import scheduler

IST = pytz.timezone('Asia/Kolkata')

# 2026-07-13 is a Monday (07-12 used pre-rewrite was a Sunday — harmless
# before since no weekday guard existed; the new guard below would now
# correctly block it, so every "trading day" case here uses a real weekday).
MONDAY = 13


def _reset():
    scheduler._advisor_running = False


def _cfg(advisor_run_now='', enc_token='tok'):
    def get(key):
        return {'advisor_run_now': advisor_run_now, 'enc_token': enc_token}.get(key)
    return get


def _fire(now, have_official=False, last_run_at=None, forced='', token='tok'):
    """Runs _maybe_run_advisor under a consistent patch set and returns the
    mocked Thread so callers can assert whether it fired."""
    with patch.object(scheduler, '_now_ist', return_value=now), \
         patch.object(config, 'QA_MODE', False), \
         patch.object(scheduler.db, 'get_config', side_effect=_cfg(advisor_run_now=forced)), \
         patch.object(scheduler.db, 'write_config'), \
         patch.object(scheduler.db, 'get_enc_token', return_value=token), \
         patch.object(scheduler, '_token_is_live', return_value=True), \
         patch.object(scheduler.db, 'has_official_advisor_run', return_value=have_official), \
         patch.object(scheduler.db, 'get_last_advisor_run_time', return_value=last_run_at), \
         patch('scheduler.threading.Thread') as T:
        scheduler._maybe_run_advisor()
    return T


def test_outside_window_no_official_yet_no_trigger():
    _reset()
    early = datetime(2026, 7, MONDAY, 8, 0, tzinfo=IST)  # before 09:45
    _fire(early, have_official=False).assert_not_called()


def test_first_run_after_window_fires_official():
    _reset()
    at_window = datetime(2026, 7, MONDAY, 10, 0, tzinfo=IST)
    _fire(at_window, have_official=False).assert_called_once()


def test_weekend_blocks_even_with_live_token_and_forced():
    _reset()
    sunday = datetime(2026, 7, 12, 10, 0, tzinfo=IST)
    _fire(sunday, have_official=False, forced='true').assert_not_called()


def test_holiday_blocks():
    _reset()
    holiday = next(iter(config.NSE_HOLIDAYS))
    y, m, d = map(int, holiday.split('-'))
    dt_ = datetime(y, m, d, 10, 0, tzinfo=IST)
    _fire(dt_, have_official=False).assert_not_called()


def test_manual_trigger_bypasses_time_window():
    _reset()
    early = datetime(2026, 7, MONDAY, 3, 0, tzinfo=IST)  # well before 09:45
    _fire(early, have_official=False, forced='true').assert_called_once()


def test_manual_trigger_consumes_flag():
    _reset()
    early = datetime(2026, 7, MONDAY, 3, 0, tzinfo=IST)
    with patch.object(scheduler, '_now_ist', return_value=early), \
         patch.object(config, 'QA_MODE', False), \
         patch.object(scheduler.db, 'get_config', side_effect=_cfg(advisor_run_now='true')), \
         patch.object(scheduler.db, 'write_config') as wc, \
         patch.object(scheduler, '_token_is_live', return_value=True), \
         patch.object(scheduler.db, 'get_enc_token', return_value='tok'), \
         patch.object(scheduler.db, 'has_official_advisor_run', return_value=False), \
         patch.object(scheduler.db, 'get_last_advisor_run_time', return_value=None), \
         patch('scheduler.threading.Thread'):
        scheduler._maybe_run_advisor()
    wc.assert_any_call('advisor_run_now', '')


def test_manual_trigger_bypasses_official_already_ran_today():
    _reset()
    at_window = datetime(2026, 7, MONDAY, 10, 0, tzinfo=IST)
    _fire(at_window, have_official=True, forced='true').assert_called_once()


def test_no_token_still_blocks_even_when_forced():
    _reset()
    now = datetime(2026, 7, MONDAY, 10, 0, tzinfo=IST)
    _fire(now, have_official=False, forced='true', token=None).assert_not_called()


def test_qa_mode_never_runs_even_when_forced():
    _reset()
    with patch.object(config, 'QA_MODE', True), \
         patch.object(scheduler.db, 'get_config', side_effect=_cfg(advisor_run_now='true')), \
         patch('scheduler.threading.Thread') as T:
        scheduler._maybe_run_advisor()
    T.assert_not_called()


# ── Regression: a failed/zero-result official run must NOT permanently
# skip the rest of the day (the 2026-07-14 bug: the old in-memory flag was
# set before the run executed, so a transient failure silently forfeited
# the whole day with no retry). Since dedup is now DB-backed
# (has_official_advisor_run reflects only rows actually written), simply
# repeating the call with have_official still False (as it would be after a
# failed run wrote nothing) keeps firing — there is no separate flag to get
# stuck.
def test_repeated_calls_keep_retrying_when_no_official_row_exists_yet():
    _reset()
    at_window = datetime(2026, 7, MONDAY, 10, 0, tzinfo=IST)
    _fire(at_window, have_official=False).assert_called_once()
    _reset()
    later = datetime(2026, 7, MONDAY, 10, 5, tzinfo=IST)
    _fire(later, have_official=False).assert_called_once()


# ── Intraday lite-refresh interval gate ─────────────────────────────────

def test_lite_run_skipped_before_interval_elapsed():
    _reset()
    now = datetime(2026, 7, MONDAY, 11, 0, tzinfo=IST)
    last_run = (now - timedelta(seconds=60)).isoformat()  # 1 min ago < 300s
    _fire(now, have_official=True, last_run_at=last_run).assert_not_called()


def test_lite_run_fires_after_interval_elapsed():
    _reset()
    now = datetime(2026, 7, MONDAY, 11, 0, tzinfo=IST)
    last_run = (now - timedelta(seconds=400)).isoformat()  # > 300s default
    _fire(now, have_official=True, last_run_at=last_run).assert_called_once()


def test_lite_run_fires_when_no_prior_run_time_known():
    _reset()
    now = datetime(2026, 7, MONDAY, 11, 0, tzinfo=IST)
    _fire(now, have_official=True, last_run_at=None).assert_called_once()


def test_lite_run_skipped_after_market_close():
    _reset()
    ch, cm = config.MARKET_CLOSE_HOUR, config.MARKET_CLOSE_MINUTE
    after_close = datetime(2026, 7, MONDAY, ch, cm, tzinfo=IST) + timedelta(minutes=1)
    _fire(after_close, have_official=True, last_run_at=None).assert_not_called()
