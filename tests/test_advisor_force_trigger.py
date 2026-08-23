"""A queued advisor request must survive a dead token. [2026-08-24]

The bug: `advisor_run_now` was consumed the moment it was READ, before the
live-token check. So pressing "re-run" with a stale token ate the request
silently -- the exact situation the button exists for. It must stay set and
fire when a live token appears.

Scope note: this does NOT test weekend behaviour. The trading-day gate applies
to forced runs too, and tests/test_advisor_trigger.py pins that on purpose --
a forced run writes an official advice batch, and a Saturday-dated batch would
have its grading horizon measured from a Saturday.
"""
from unittest.mock import MagicMock, patch

import scheduler


def _run(forced, token_live):
    d = MagicMock()
    d.weekday.return_value = 0                    # Monday, a trading day
    d.hour, d.minute = 20, 0                      # past the 09:45 window
    d.date.return_value.isoformat.return_value = '2026-08-24'
    cfg = {'advisor_run_now': 'true' if forced else ''}
    with patch.object(scheduler, '_now_ist', return_value=d), \
         patch.object(scheduler.config, 'QA_MODE', False), \
         patch.object(scheduler.config, 'NSE_HOLIDAYS', []), \
         patch.object(scheduler.db, 'get_config', side_effect=lambda k: cfg.get(k, '')), \
         patch.object(scheduler.db, 'write_config',
                      side_effect=lambda k, v: cfg.__setitem__(k, v)), \
         patch.object(scheduler.db, 'has_official_advisor_run', return_value=False), \
         patch.object(scheduler.db, 'get_enc_token', return_value='tok'), \
         patch.object(scheduler, '_token_is_live', return_value=token_live), \
         patch.object(scheduler.threading, 'Thread') as th:
        scheduler._advisor_running = False
        scheduler._maybe_run_advisor()
    return cfg, th


def test_request_is_kept_when_the_token_is_dead():
    cfg, th = _run(forced=True, token_live=False)
    assert cfg['advisor_run_now'] == 'true', 'forced request was eaten by a dead token'
    th.assert_not_called()


def test_request_is_consumed_once_the_run_actually_starts():
    cfg, th = _run(forced=True, token_live=True)
    assert cfg['advisor_run_now'] == ''
    assert th.call_count == 1
