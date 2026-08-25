"""Intraday candle windows must honour the requested lookback. [C7]

_get_historical used to ignore `days` for 5minute/15minute and hardcode a fixed
window. data_jobs asked for days=5 to build the in-play list and silently got 3
CALENDAR days -- which after a weekend holds at most one prior trading day.
opening_range_stats needs >= 2 to compute an RVOL baseline, so every candidate
came back or_rvol=None, the ranking was empty, and inplay_list never locked.

Mid-week it worked; every Monday it did not. 08-05/06/07 locked, 08-10, 08-24
and 08-25 did not.
"""
import os
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

# Guarded import, matching the other db-touching tests: importing market_data
# bare at module scope pulls in database/config side effects early enough to
# perturb tests that patch them later (it broke
# test_scheduler.py::test_start_session_creation_failed in full-suite order).
with patch.dict(os.environ, {
    'SUPABASE_URL': 'https://fake.supabase.co',
    'SUPABASE_SERVICE_KEY': 'fake-key',
}):
    with patch('supabase.create_client', return_value=MagicMock()):
        from market_data import MarketData


def _captured_from(interval, days, now):
    md = MarketData.__new__(MarketData)
    md._now = lambda: now
    md.kite = MagicMock()
    md.kite._get.return_value = {'candles': []}
    md._get_historical(123, interval, days)
    params = md.kite._get.call_args.kwargs['params']
    return datetime.strptime(params['from'], '%Y-%m-%d %H:%M:%S')


NOW = datetime(2026, 8, 25, 12, 29, 0)      # a Tuesday, mid-session


def test_5minute_honours_a_larger_request():
    """days=5 must actually reach back 5 days -- the in-play lock depends on it."""
    frm = _captured_from('5minute', 5, NOW)
    assert (NOW - frm) >= timedelta(days=5)


def test_5minute_keeps_its_floor_for_small_requests():
    """Callers asking days=1 previously got 3; they must keep getting 3, or
    intraday paths that relied on the old behaviour would silently narrow."""
    frm = _captured_from('5minute', 1, NOW)
    assert (NOW - frm) >= timedelta(days=3)


def test_window_starts_at_midnight_so_the_oldest_day_is_whole():
    """Measuring back from `now` clipped the oldest day's opening range, so a
    '3 day' window really held only 2 usable ones."""
    frm = _captured_from('5minute', 5, NOW)
    assert (frm.hour, frm.minute, frm.second) == (0, 0, 0)


def test_daily_interval_is_untouched():
    frm = _captured_from('day', 5, NOW)
    assert (NOW - frm) >= timedelta(days=399)


def test_day_honours_a_larger_request():
    """The 'day' branch hardcoded 400 and ignored `days`, capping every caller
    at ~271 bars however much it asked for -- the same defect as the intraday
    one above, just quieter. The advisor replay lab needs multi-year history."""
    frm = _captured_from('day', 1900, NOW)
    assert (NOW - frm) >= timedelta(days=1900)


def test_day_keeps_its_400_floor():
    """Production asks for exactly 400 and EMA200 needs that depth; a smaller
    request must not shrink the window below it."""
    frm = _captured_from('day', 30, NOW)
    assert (NOW - frm) >= timedelta(days=400)
