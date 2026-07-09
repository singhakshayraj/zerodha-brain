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

from kite_client import TokenExpiredError

_UNSET = object()  # sentinel so we can pass explicit None


def _make_brain(initialized=True, session_ended=False):
    b = MagicMock()
    b.initialize.return_value = initialized
    b._session_ended = session_ended
    b.cycle_count = 0
    b.session_stats = {'trades_executed': 0, 'total_pnl': 0.0}
    return b


def _run_scheduler(commands, token='tok', session_config=_UNSET,
                   session_row=_UNSET, market_open=True, brain=_UNSET,
                   token_live=True):
    """
    Run scheduler.run() for a bounded command sequence.
    Raises KeyboardInterrupt after commands exhausted → stops loop.
    """
    import scheduler
    scheduler._is_trading = False  # reset global
    scheduler._stale_token_reported = False

    if session_config is _UNSET:
        session_config = {'capitalDeployed': 10000, 'maxLossPercent': 5, 'tradeIntervalSeconds': 1}
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
         patch('scheduler._token_is_live', return_value=token_live), \
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


# --- START: stale token → NO session created (2026-07-09 lost-morning fix) ---

def _run_start_once(token_live, brain=None, capture_writes=None,
                    on_create=None):
    """Run scheduler.run() through a single START with full mocks. Lets a
    test observe create_session / write_config without _run_scheduler's own
    patches shadowing them."""
    import scheduler
    scheduler._is_trading = False
    scheduler._stale_token_reported = False
    brain = brain or _make_brain()
    cmds = ['START']

    def get_cmd():
        if cmds:
            return cmds.pop(0)
        raise KeyboardInterrupt

    def _create(cfg):
        if on_create:
            on_create(cfg)
        return {'id': 'sess-x'}

    def _wc(k, v):
        if capture_writes is not None:
            capture_writes[k] = v

    with patch('scheduler.db.get_brain_command', side_effect=get_cmd), \
         patch('scheduler.db.get_enc_token', return_value='tok'), \
         patch('scheduler.db.get_session_config',
               return_value={'capitalDeployed': 10000, 'maxLossPercent': 5,
                             'tradeIntervalSeconds': 1}), \
         patch('scheduler.db.create_session', side_effect=_create), \
         patch('scheduler.db.write_config', side_effect=_wc), \
         patch('scheduler.db.update_heartbeat'), \
         patch('scheduler.risk_manager.is_market_open', return_value=True), \
         patch('scheduler.TradingBrain', return_value=brain), \
         patch('scheduler._token_is_live', return_value=token_live), \
         patch('scheduler.time.sleep'):
        try:
            scheduler.run()
        except (KeyboardInterrupt, StopIteration):
            pass
    return brain


def test_stale_token_creates_no_session():
    """A stale token at start must not spawn a doomed session — that row would
    trip has_session_today() and suppress autopilot for the rest of the day."""
    brain = _make_brain()
    created = []
    _run_start_once(token_live=False, brain=brain,
                    on_create=lambda cfg: created.append(cfg))
    assert created == []              # no session row created
    brain.initialize.assert_not_called()


def test_stale_token_writes_incident_and_idles():
    writes = {}
    _run_start_once(token_live=False, capture_writes=writes)
    assert 'token_incident' in writes
    assert writes.get('brain_status') == 'IDLE'


def test_live_token_creates_session():
    brain = _make_brain()
    created = []
    _run_start_once(token_live=True, brain=brain,
                    on_create=lambda cfg: created.append(cfg))
    assert len(created) == 1          # session created when token is live
    brain.initialize.assert_called_once()


def test_live_token_still_starts_normally():
    brain = _make_brain()
    _run_scheduler(['START', 'STOP'], brain=brain, token_live=True)
    brain.initialize.assert_called_once()


# --- _token_is_live probe semantics ---

def test_token_is_live_true_in_qa_mode(monkeypatch):
    import scheduler
    monkeypatch.setattr(scheduler.config, 'QA_MODE', True)
    assert scheduler._token_is_live('anything') is True


def test_token_is_live_false_on_token_expired(monkeypatch):
    import scheduler
    monkeypatch.setattr(scheduler.config, 'QA_MODE', False)
    kite = MagicMock()
    kite.get_profile.side_effect = TokenExpiredError('expired')
    with patch('scheduler.KiteClient', return_value=kite):
        assert scheduler._token_is_live('stale') is False


def test_token_is_live_true_on_transient_error(monkeypatch):
    # A network blip must NOT be read as a dead token (would strand the brain)
    import scheduler
    monkeypatch.setattr(scheduler.config, 'QA_MODE', False)
    kite = MagicMock()
    kite.get_profile.side_effect = RuntimeError('connection reset')
    with patch('scheduler.KiteClient', return_value=kite):
        assert scheduler._token_is_live('maybe') is True


def test_token_is_live_true_on_success(monkeypatch):
    import scheduler
    monkeypatch.setattr(scheduler.config, 'QA_MODE', False)
    kite = MagicMock()
    kite.get_profile.return_value = {'user_id': 'AB1234'}
    with patch('scheduler.KiteClient', return_value=kite):
        assert scheduler._token_is_live('good') is True


def test_report_stale_token_dedupes():
    import scheduler
    scheduler._stale_token_reported = False
    writes = []
    with patch('scheduler.db.write_config',
               side_effect=lambda k, v: writes.append(k)), \
         patch('scheduler._set_heartbeat'):
        scheduler._report_stale_token()
        scheduler._report_stale_token()
    assert writes.count('token_incident') == 1   # durable incident written once


# --- START: init failure must release the session (2026-07-09 zombie fix) ---

def test_start_initialize_false_aborts_session_and_clears_pointer():
    """A failed init (e.g. expired token) must mark the DB session ABORTED and
    clear active_session_id — otherwise the row stays RUNNING and the pointer
    stays set, a zombie that silently blocks every later Start."""
    import scheduler
    scheduler._is_trading = False
    brain = _make_brain(initialized=False)
    cmds = ['START']

    def get_cmd():
        if cmds:
            return cmds.pop(0)
        raise KeyboardInterrupt

    with patch('scheduler.db.get_brain_command', side_effect=get_cmd), \
         patch('scheduler.db.get_enc_token', return_value='tok'), \
         patch('scheduler.db.get_session_config',
               return_value={'capitalDeployed': 10000, 'maxLossPercent': 5,
                             'tradeIntervalSeconds': 1}), \
         patch('scheduler.db.create_session', return_value={'id': 'sess-zombie'}), \
         patch('scheduler.db.update_session') as upd, \
         patch('scheduler.db.write_config') as wc, \
         patch('scheduler.db.update_heartbeat'), \
         patch('scheduler.risk_manager.is_market_open', return_value=True), \
         patch('scheduler.TradingBrain', return_value=brain), \
         patch('scheduler.time.sleep'):
        try:
            scheduler.run()
        except (KeyboardInterrupt, StopIteration):
            pass

    # session marked ABORTED / INIT_FAILED
    upd.assert_called_once()
    assert upd.call_args.args[0] == 'sess-zombie'
    assert upd.call_args.args[1]['status'] == 'ABORTED'
    assert upd.call_args.args[1]['end_reason'] == 'INIT_FAILED'
    # active_session_id cleared to ''
    cleared = [c for c in wc.call_args_list
               if c.args and c.args[0] == 'active_session_id' and c.args[1] == '']
    assert cleared, "active_session_id was not cleared after init failure"


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
         patch('scheduler.db.get_session_config', return_value={'capitalDeployed': 10000, 'maxLossPercent': 5, 'tradeIntervalSeconds': 1}), \
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
         patch('scheduler.db.get_session_config', return_value={'capitalDeployed': 10000, 'maxLossPercent': 5, 'tradeIntervalSeconds': 1}), \
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
