"""_maybe_run_advisor gating: daily time window + once-per-day dedup, and the
manual 'advisor_run_now' trigger that bypasses both for an on-demand run."""
import os
from datetime import datetime
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


def _reset():
    scheduler._advisor_date = None
    scheduler._advisor_running = False


def _cfg(advisor_run_now='', enc_token='tok'):
    def get(key):
        return {'advisor_run_now': advisor_run_now, 'enc_token': enc_token}.get(key)
    return get


def test_outside_window_no_trigger_does_nothing():
    _reset()
    early = datetime(2026, 7, 12, 8, 0, tzinfo=IST)  # before 09:20
    with patch('scheduler.datetime') as dt, \
         patch.object(config, 'QA_MODE', False), \
         patch.object(scheduler.db, 'get_config', side_effect=_cfg()), \
         patch.object(scheduler.db, 'write_config'), \
         patch.object(scheduler.db, 'get_enc_token', return_value='tok'), \
         patch('scheduler.threading.Thread') as T:
        dt.now.return_value = early
        scheduler._maybe_run_advisor()
    T.assert_not_called()


def test_manual_trigger_bypasses_time_window():
    _reset()
    early = datetime(2026, 7, 12, 3, 0, tzinfo=IST)  # well before 09:20
    with patch('scheduler.datetime') as dt, \
         patch.object(config, 'QA_MODE', False), \
         patch.object(scheduler.db, 'get_config', side_effect=_cfg(advisor_run_now='true')), \
         patch.object(scheduler.db, 'write_config') as wc, \
         patch.object(scheduler, '_token_is_live', return_value=True), \
         patch.object(scheduler.db, 'get_enc_token', return_value='tok'), \
         patch('scheduler.threading.Thread') as T:
        dt.now.return_value = early
        scheduler._maybe_run_advisor()
    T.assert_called_once()
    # trigger consumed (cleared) so it fires once
    wc.assert_any_call('advisor_run_now', '')


def test_manual_trigger_bypasses_once_per_day_dedup():
    _reset()
    scheduler._advisor_date = datetime(2026, 7, 12, tzinfo=IST).date().isoformat()
    at_window = datetime(2026, 7, 12, 10, 0, tzinfo=IST)
    with patch('scheduler.datetime') as dt, \
         patch.object(config, 'QA_MODE', False), \
         patch.object(scheduler.db, 'get_config', side_effect=_cfg(advisor_run_now='true')), \
         patch.object(scheduler.db, 'write_config'), \
         patch.object(scheduler, '_token_is_live', return_value=True), \
         patch.object(scheduler.db, 'get_enc_token', return_value='tok'), \
         patch('scheduler.threading.Thread') as T:
        dt.now.return_value = at_window
        scheduler._maybe_run_advisor()
    T.assert_called_once()


def test_no_token_still_blocks_even_when_forced():
    _reset()
    now = datetime(2026, 7, 12, 10, 0, tzinfo=IST)
    with patch('scheduler.datetime') as dt, \
         patch.object(config, 'QA_MODE', False), \
         patch.object(scheduler.db, 'get_config', side_effect=_cfg(advisor_run_now='true')), \
         patch.object(scheduler.db, 'write_config'), \
         patch.object(scheduler.db, 'get_enc_token', return_value=None), \
         patch('scheduler.threading.Thread') as T:
        dt.now.return_value = now
        scheduler._maybe_run_advisor()
    T.assert_not_called()


def test_qa_mode_never_runs_even_when_forced():
    _reset()
    with patch.object(config, 'QA_MODE', True), \
         patch.object(scheduler.db, 'get_config', side_effect=_cfg(advisor_run_now='true')), \
         patch('scheduler.threading.Thread') as T:
        scheduler._maybe_run_advisor()
    T.assert_not_called()
