"""Circuit-breaker streak correctness + re-entry cooldown (2026-07-09 review).

Two findings from the 2026-07-09 session:
  - the consecutive-loss counter only advanced on STOP_LOSS_HIT, so a losing
    time-stop / session-end close never counted → the breaker undercounted;
  - KOTAKBANK was re-shorted ~6min after it stopped out, into a second loss.
"""
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
from brain import TradingBrain

IST = pytz.timezone('Asia/Kolkata')


def _brain():
    b = TradingBrain.__new__(TradingBrain)
    b.session_id = 's1'
    b.consecutive_losses = 0
    b._last_loss_exit = {}
    return b


# --- _record_close_outcome: streak counts ANY realized loss ---

def test_loss_extends_streak_regardless_of_exit_path():
    b = _brain()
    b._record_close_outcome('INFY', -10)   # a losing close (any reason)
    b._record_close_outcome('TCS', -5)
    assert b.consecutive_losses == 2


def test_win_resets_streak():
    b = _brain()
    b.consecutive_losses = 2
    b._record_close_outcome('INFY', 30)
    assert b.consecutive_losses == 0


def test_breakeven_counts_as_loss_side():
    # pnl == 0 is not a win — it extends the streak (matches losing_trades)
    b = _brain()
    b._record_close_outcome('INFY', 0)
    assert b.consecutive_losses == 1


def test_losing_close_records_cooldown_timestamp():
    b = _brain()
    b._record_close_outcome('KOTAKBANK', -80)
    assert 'KOTAKBANK' in b._last_loss_exit


def test_winning_close_sets_no_cooldown():
    b = _brain()
    b._record_close_outcome('KOTAKBANK', 40)
    assert 'KOTAKBANK' not in b._last_loss_exit


# --- _reentry_cooldown window math ---

def test_reentry_blocked_within_window():
    b = _brain()
    b._last_loss_exit['KOTAKBANK'] = datetime.now(IST) - timedelta(minutes=6)
    blocked, mins = b._reentry_cooldown('KOTAKBANK')
    assert blocked is True and 5 <= mins <= 7


def test_reentry_allowed_after_window():
    b = _brain()
    b._last_loss_exit['KOTAKBANK'] = datetime.now(IST) - timedelta(
        minutes=config.REENTRY_COOLDOWN_MIN + 1)
    blocked, _ = b._reentry_cooldown('KOTAKBANK')
    assert blocked is False


def test_reentry_no_prior_loss_never_blocks():
    b = _brain()
    blocked, mins = b._reentry_cooldown('NEVERTRADED')
    assert blocked is False and mins is None


# --- _cooldown_gate: flag-gated act vs counterfactual ---

def test_cooldown_gate_blocks_when_enabled(monkeypatch):
    monkeypatch.setattr(config, 'REENTRY_COOLDOWN_ENABLED', True)
    b = _brain()
    b._last_loss_exit['KOTAKBANK'] = datetime.now(IST) - timedelta(minutes=3)
    with patch('brain.db.log_brain_activity') as log:
        skip = b._cooldown_gate('KOTAKBANK')
    assert skip is True
    assert log.call_args.kwargs['activity_type'] == 'REENTRY_BLOCKED'


def test_cooldown_gate_allows_but_logs_counterfactual_when_disabled(monkeypatch):
    monkeypatch.setattr(config, 'REENTRY_COOLDOWN_ENABLED', False)
    b = _brain()
    b._last_loss_exit['KOTAKBANK'] = datetime.now(IST) - timedelta(minutes=3)
    with patch('brain.db.log_brain_activity') as log:
        skip = b._cooldown_gate('KOTAKBANK')
    assert skip is False   # flag off → still enters
    assert log.call_args.kwargs['activity_type'] == 'REENTRY_WOULD_BLOCK'


def test_cooldown_gate_noop_without_prior_loss(monkeypatch):
    monkeypatch.setattr(config, 'REENTRY_COOLDOWN_ENABLED', True)
    b = _brain()
    with patch('brain.db.log_brain_activity') as log:
        skip = b._cooldown_gate('FRESH')
    assert skip is False
    log.assert_not_called()
