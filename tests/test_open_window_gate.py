"""P-07 / T4: trade-only-open dark filter. T4 found the opening hour is the
only +EV bucket; expectancy falls monotonically through the day. _open_window_gate
logs, per entry after the open window, what a trade-only-open filter WOULD have
suppressed — and only actually blocks when the flag is on (measure before enable)."""
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
from brain import TradingBrain

IST = pytz.timezone('Asia/Kolkata')


def _brain():
    b = TradingBrain.__new__(TradingBrain)
    b.session_id = 's1'
    b._outside_open_logged = set()
    return b


def _at(hh, mm):
    """Patch brain's clock to a fixed IST wall time (real datetime, so the
    gate's .replace()/subtraction work)."""
    fixed = IST.localize(datetime(2026, 8, 3, hh, mm))
    m = MagicMock(wraps=datetime)
    m.now.return_value = fixed
    return patch('brain.datetime', m)


def test_inside_window_always_allows_no_log():
    b = _brain()
    with _at(9, 45), patch('brain.db.log_brain_activity') as log:
        skip = b._open_window_gate('INFY', 'BUY')
    assert skip is False
    log.assert_not_called()


def test_after_window_disabled_allows_but_logs_counterfactual(monkeypatch):
    monkeypatch.setattr(config, 'TRADE_ONLY_OPEN_ENABLED', False)
    b = _brain()
    with _at(11, 0), patch('brain.db.log_brain_activity') as log:
        skip = b._open_window_gate('INFY', 'BUY')
    assert skip is False   # flag off → still enters
    assert log.call_args.kwargs['activity_type'] == 'OUTSIDE_OPEN_WOULD_BLOCK'


def test_after_window_enabled_blocks(monkeypatch):
    monkeypatch.setattr(config, 'TRADE_ONLY_OPEN_ENABLED', True)
    b = _brain()
    with _at(11, 0), patch('brain.db.log_brain_activity') as log:
        skip = b._open_window_gate('INFY', 'SHORT')
    assert skip is True
    assert log.call_args.kwargs['activity_type'] == 'OUTSIDE_OPEN_BLOCKED'


def test_would_block_deduped_per_symbol(monkeypatch):
    monkeypatch.setattr(config, 'TRADE_ONLY_OPEN_ENABLED', False)
    b = _brain()
    with _at(11, 0), patch('brain.db.log_brain_activity') as log:
        b._open_window_gate('INFY', 'BUY')
        b._open_window_gate('INFY', 'BUY')   # same symbol again same session
    assert log.call_count == 1   # deduped


def test_boundary_at_window_end_allows(monkeypatch):
    monkeypatch.setattr(config, 'TRADE_ONLY_OPEN_ENABLED', True)
    b = _brain()
    with _at(10, 15), patch('brain.db.log_brain_activity') as log:
        skip = b._open_window_gate('INFY', 'BUY')   # exactly at 10:15 end
    assert skip is False
    log.assert_not_called()
