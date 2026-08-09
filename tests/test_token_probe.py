"""Temporary enctoken-expiry experiment (2026-08-10).

These tests exist for one reason: this is experiment code living inside the
production trading brain, so the properties that make it SAFE must be pinned,
not assumed. Specifically it must be a no-op on any other date, must stop before
the open, and must never raise into the scheduler loop.
"""
from unittest.mock import MagicMock, patch

import scheduler


def _clock(date_iso, hour, minute=0):
    d = MagicMock()
    d.date.return_value.isoformat.return_value = date_iso
    d.hour, d.minute = hour, minute
    d.strftime.return_value = f"{hour:02d}:{minute:02d}"
    return patch.object(scheduler, 'datetime', MagicMock(now=MagicMock(return_value=d)))


def _reset():
    scheduler._token_probe_last = 0.0
    scheduler._token_probe_done = False


def test_noop_on_any_other_date():
    """The single most important property: if nobody removes this, it must be
    dead code tomorrow rather than a permanent extra API call on every tick."""
    _reset()
    with _clock('2026-08-11', 3), patch.object(scheduler.db, 'get_enc_token') as tok:
        scheduler._maybe_probe_token_expiry()
    tok.assert_not_called()


def test_stops_before_the_open():
    _reset()
    with _clock('2026-08-10', 9), patch.object(scheduler.db, 'get_enc_token') as tok:
        scheduler._maybe_probe_token_expiry()
    tok.assert_not_called()


def test_records_ok_inside_the_window():
    _reset()
    writes = {}
    with _clock('2026-08-10', 3), \
         patch.object(scheduler.db, 'get_enc_token', return_value='t'), \
         patch.object(scheduler.db, 'get_config', return_value=''), \
         patch.object(scheduler.db, 'write_config',
                      side_effect=lambda k, v: writes.__setitem__(k, v)), \
         patch.object(scheduler, 'KiteClient', return_value=MagicMock()):
        scheduler._maybe_probe_token_expiry()
    assert 'OK' in writes['token_probe_log']


def test_death_is_recorded_and_stops_the_probe():
    _reset()
    writes = {}
    with _clock('2026-08-10', 6, 15), \
         patch.object(scheduler.db, 'get_enc_token', return_value='t'), \
         patch.object(scheduler.db, 'get_config', return_value='03:00 OK'), \
         patch.object(scheduler.db, 'write_config',
                      side_effect=lambda k, v: writes.__setitem__(k, v)), \
         patch.object(scheduler, 'KiteClient') as kc:
        kc.return_value.get_profile.side_effect = scheduler.TokenExpiredError('x')
        scheduler._maybe_probe_token_expiry()

    log = writes['token_probe_log']
    assert 'DEAD' in log and 'DIED' in log
    assert '03:00 OK' in log                 # prior history preserved
    assert scheduler._token_probe_done is True

    # and it must not keep probing after that
    with _clock('2026-08-10', 6, 30), patch.object(scheduler.db, 'get_enc_token') as tok:
        scheduler._maybe_probe_token_expiry()
    tok.assert_not_called()


def test_network_blip_is_not_recorded_as_death():
    """The one way this experiment could produce a confidently wrong answer."""
    _reset()
    writes = {}
    with _clock('2026-08-10', 4), \
         patch.object(scheduler.db, 'get_enc_token', return_value='t'), \
         patch.object(scheduler.db, 'get_config', return_value=''), \
         patch.object(scheduler.db, 'write_config',
                      side_effect=lambda k, v: writes.__setitem__(k, v)), \
         patch.object(scheduler, 'KiteClient') as kc:
        kc.return_value.get_profile.side_effect = ConnectionError('boom')
        scheduler._maybe_probe_token_expiry()
    assert 'INCONCLUSIVE' in writes['token_probe_log']
    assert 'DEAD' not in writes['token_probe_log']
    assert scheduler._token_probe_done is False


def test_never_raises_into_the_scheduler_loop():
    _reset()
    with _clock('2026-08-10', 4), \
         patch.object(scheduler.db, 'get_enc_token', side_effect=Exception('db down')):
        scheduler._maybe_probe_token_expiry()   # must not raise
