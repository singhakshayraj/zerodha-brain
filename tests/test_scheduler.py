"""T2.3 — Scheduler unit tests. All db + brain calls mocked."""
import pytest
from unittest.mock import MagicMock, patch
import threading
import os

with patch.dict(os.environ, {
    'SUPABASE_URL': 'https://fake.supabase.co',
    'SUPABASE_SERVICE_KEY': 'fake-key',
}):
    with patch('supabase.create_client', return_value=MagicMock()):
        import database  # noqa

_UNSET = object()  # sentinel so we can pass explicit None


def _make_brain(initialized=True, session_ended=False):
    b = MagicMock()
    b.initialize.return_value = initialized
    b._session_ended = session_ended
    b.cycle_count = 0
    b.session_stats = {'trades_executed': 0, 'total_pnl': 0.0}
    return b


def _run_scheduler(commands, token='tok', session_config=_UNSET,
                   session_row=_UNSET, market_open=True, brain=_UNSET):
    """
    Run scheduler.run() for a bounded command sequence.
    Raises KeyboardInterrupt after commands exhausted → stops loop.
    """
    import scheduler
    scheduler._is_trading = False  # reset global

    if session_config is _UNSET:
        session_config = {'capitalDeployed': 10000, 'tradeIntervalSeconds': 1}
    if session_row is _UNSET:
        session_row = {'id': 'sess-sch-001'}
    if brain is _UNSET:
        brain = _make_brain()

    cmds = list(commands)

    def get_cmd():
        if cmds:
            return cmds.pop(0)
        raise KeyboardInterrupt

    with patch('scheduler.db.get_brain_command', side_effect=get_cmd), \
         patch('scheduler.db.get_enc_token', return_value=token), \
         patch('scheduler.db.get_session_config', return_value=session_config), \
         patch('scheduler.db.create_session', return_value=session_row), \
         patch('scheduler.db.write_config'), \
         patch('scheduler.db.update_heartbeat'), \
         patch('scheduler.risk_manager.is_market_open', return_value=market_open), \
         patch('scheduler.TradingBrain', return_value=brain), \
         patch('scheduler.time.sleep'):
        try:
            scheduler.run()
        except (KeyboardInterrupt, StopIteration):
            pass
    return brain


# --- START: no token → error, no session started ---

def test_start_no_token_no_session():
    brain = _run_scheduler(['START'], token=None)
    brain.initialize.assert_not_called()


# --- START: no session_config → error ---

def test_start_no_session_config_no_session():
    brain = _run_scheduler(['START'], session_config=None)
    brain.initialize.assert_not_called()


# --- START: session creation returns None → no trade loop ---

def test_start_session_creation_failed():
    brain = _run_scheduler(['START'], session_row=None)
    brain.initialize.assert_not_called()


# --- START: brain.initialize returns False → no run_cycle ---

def test_start_initialize_false_no_trade_loop():
    brain = _make_brain(initialized=False)
    _run_scheduler(['START'], brain=brain)
    brain.run_cycle.assert_not_called()


# --- START success → brain initialized ---

def test_start_success_brain_initialized():
    # Commands: START (outer), then STOP (inner loop) → clean exit
    brain = _make_brain()
    _run_scheduler(['START', 'STOP'], brain=brain)
    brain.initialize.assert_called_once()


# --- STOP command during trade loop → exits, calls end_session ---

def test_stop_command_exits_loop():
    brain = _make_brain()
    _run_scheduler(['START', 'STOP'], brain=brain)
    brain.end_session.assert_called_with('MANUAL_STOP')


# --- market closed during trade loop → exits loop ---

def test_market_closed_exits_loop():
    brain = _make_brain()
    # START → inner loop: market check fires before get_brain_command (2nd call)
    # But scheduler calls get_brain_command first in inner loop.
    # We only give 'START'; after inner loop checks market=False, exits.
    # However, inner loop calls get_brain_command() first → KeyboardInterrupt
    # So we need to give enough commands. Let's give START + one 'RUNNING' command.
    _run_scheduler(['START', 'RUNNING'], market_open=False, brain=brain)
    brain.end_session.assert_called_with('MARKET_CLOSED')


# --- session_ended=True → exits loop without calling run_cycle ---

def test_session_ended_exits_without_run_cycle():
    brain = _make_brain(session_ended=True)
    _run_scheduler(['START', 'RUNNING'], brain=brain)
    brain.run_cycle.assert_not_called()


# --- brain restart mid-session: resume, don't duplicate (silent-failure fix) ---

def test_running_command_resumes_existing_session_without_duplicating():
    """A fresh process reading command=='RUNNING' (brain restart mid-session,
    e.g. Railway redeploy) must resume the already-active session rather than
    creating a second one or idling forever with the old session stuck."""
    brain = _make_brain()
    existing_session = {'id': 'existing-sess-999', 'status': 'RUNNING'}

    import scheduler
    scheduler._is_trading = False
    cmds = ['RUNNING', 'STOP']

    def get_cmd():
        if cmds:
            return cmds.pop(0)
        raise KeyboardInterrupt

    def get_config_side_effect(key):
        if key == 'active_session_id':
            return 'existing-sess-999'
        return None

    with patch('scheduler.db.get_brain_command', side_effect=get_cmd), \
         patch('scheduler.db.get_enc_token', return_value='tok'), \
         patch('scheduler.db.get_session_config', return_value={'capitalDeployed': 10000, 'tradeIntervalSeconds': 1}), \
         patch('scheduler.db.get_config', side_effect=get_config_side_effect), \
         patch('scheduler.db.get_session_by_id', return_value=existing_session), \
         patch('scheduler.db.create_session') as mock_create, \
         patch('scheduler.db.write_config'), \
         patch('scheduler.db.update_heartbeat'), \
         patch('scheduler.risk_manager.is_market_open', return_value=True), \
         patch('scheduler.TradingBrain', return_value=brain), \
         patch('scheduler.time.sleep'):
        try:
            scheduler.run()
        except (KeyboardInterrupt, StopIteration):
            pass

    mock_create.assert_not_called()
    brain.resume_stats.assert_called_once_with('existing-sess-999')


def test_start_command_with_no_existing_session_creates_new_one():
    """Plain START with nothing active still creates a session normally —
    the resume path must not interfere with the ordinary first-start flow."""
    brain = _make_brain()

    def get_config_side_effect(key):
        return None  # no active_session_id set yet

    with patch('scheduler.db.get_brain_command', side_effect=['START', 'STOP', KeyboardInterrupt]), \
         patch('scheduler.db.get_enc_token', return_value='tok'), \
         patch('scheduler.db.get_session_config', return_value={'capitalDeployed': 10000, 'tradeIntervalSeconds': 1}), \
         patch('scheduler.db.get_config', side_effect=get_config_side_effect), \
         patch('scheduler.db.get_session_by_id', return_value=None), \
         patch('scheduler.db.create_session', return_value={'id': 'new-sess-1'}) as mock_create, \
         patch('scheduler.db.write_config'), \
         patch('scheduler.db.update_heartbeat'), \
         patch('scheduler.risk_manager.is_market_open', return_value=True), \
         patch('scheduler.TradingBrain', return_value=brain), \
         patch('scheduler.time.sleep'):
        import scheduler
        scheduler._is_trading = False
        try:
            scheduler.run()
        except (KeyboardInterrupt, StopIteration):
            pass

    mock_create.assert_called_once()
    brain.resume_stats.assert_not_called()


# --- Exception in outer loop → logs error, continues ---

def test_exception_in_main_loop_logs_error(capsys):
    import scheduler
    scheduler._is_trading = False

    call_n = {'n': 0}

    def get_cmd():
        call_n['n'] += 1
        if call_n['n'] == 1:
            raise RuntimeError("DB connection lost")
        raise KeyboardInterrupt

    with patch('scheduler.db.get_brain_command', side_effect=get_cmd), \
         patch('scheduler.db.get_enc_token', return_value='tok'), \
         patch('scheduler.db.get_session_config', return_value=None), \
         patch('scheduler.db.create_session', return_value=None), \
         patch('scheduler.db.write_config'), \
         patch('scheduler.db.update_heartbeat'), \
         patch('scheduler.risk_manager.is_market_open', return_value=False), \
         patch('scheduler.TradingBrain', return_value=MagicMock()), \
         patch('scheduler.time.sleep'):
        try:
            scheduler.run()
        except KeyboardInterrupt:
            pass

    captured = capsys.readouterr()
    assert 'error' in captured.out.lower() or 'Error' in captured.out


# --- heartbeat thread calls update_heartbeat ---

def test_heartbeat_thread_calls_update_heartbeat():
    import scheduler

    call_log = []
    done = threading.Event()

    def fake_update(s, c, m):
        call_log.append((s, c, m))
        done.set()

    def fake_sleep(n):
        if done.is_set():
            raise KeyboardInterrupt

    with patch('scheduler.db.update_heartbeat', side_effect=fake_update), \
         patch('scheduler.time.sleep', side_effect=fake_sleep):
        t = threading.Thread(target=scheduler._heartbeat_thread, daemon=True)
        t.start()
        done.wait(timeout=3)
        t.join(timeout=1)

    assert len(call_log) >= 1
