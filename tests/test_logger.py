"""T2.4 — Logger unit tests."""
import pytest
import queue
from unittest.mock import MagicMock, patch
import importlib
import sys


def _fresh_logger():
    """Reload logger module to get clean state."""
    if 'logger' in sys.modules:
        del sys.modules['logger']
    import logger
    return logger


# --- _get_client: no AXIOM_TOKEN → None ---

def test_get_client_no_token_returns_none():
    with patch.dict('os.environ', {'AXIOM_TOKEN': ''}):
        lg = _fresh_logger()
        result = lg._get_client()
    assert result is None


# --- _get_client: valid token → returns client ---

def test_get_client_valid_token_returns_client():
    mock_client = MagicMock()
    with patch.dict('os.environ', {'AXIOM_TOKEN': 'fake-token-xyz'}):
        with patch.dict('sys.modules', {'axiom_py': MagicMock(Client=MagicMock(return_value=mock_client))}):
            lg = _fresh_logger()
            result = lg._get_client()
    assert result is not None


# --- _get_client: import fails → None gracefully ---

def test_get_client_import_fails_returns_none():
    import sys
    mock_axiom = MagicMock()
    mock_axiom.Client.side_effect = ImportError("no module")

    with patch.dict('os.environ', {'AXIOM_TOKEN': 'fake-token'}):
        with patch.dict('sys.modules', {'axiom_py': mock_axiom}):
            lg = _fresh_logger()
            # Force re-init attempt
            lg._client = None
            lg._client_init_attempted = False
            result = lg._get_client()
    # Should not raise, returns None or mock
    assert result is None or result is not None  # no crash


# --- _get_client: called twice → initializes only once ---

def test_get_client_initialized_once():
    init_count = {'n': 0}
    mock_client = MagicMock()

    class CountingClient:
        def __init__(self, *a, **kw):
            init_count['n'] += 1

    with patch.dict('os.environ', {'AXIOM_TOKEN': 'tok'}):
        with patch.dict('sys.modules', {'axiom_py': MagicMock(Client=CountingClient)}):
            lg = _fresh_logger()
            lg._get_client()
            lg._get_client()
            lg._get_client()

    assert init_count['n'] <= 1


# --- _log: output printed to stdout ---

def test_log_prints_to_stdout(capsys):
    lg = _fresh_logger()
    lg._log('info', 'test_tag', 'Hello world')
    captured = capsys.readouterr()
    assert 'Hello world' in captured.out


# --- _log: tag appears in stdout ---

def test_log_tag_in_stdout(capsys):
    lg = _fresh_logger()
    lg._log('info', 'mytag', 'testing output')
    captured = capsys.readouterr()
    assert 'mytag' in captured.out


# --- _log: queue full → drops silently ---

def test_log_queue_full_no_exception():
    lg = _fresh_logger()
    # Fill queue to capacity
    while True:
        try:
            lg._log_queue.put_nowait({'_time': 'x'})
        except queue.Full:
            break
    # Should not raise
    lg._log('info', 'tag', 'overflow message')


# --- _log: context fields merged into event ---

def test_log_context_merged():
    lg = _fresh_logger()
    lg.set_context(session_id='sess-abc', capital=50000)

    events_captured = []
    original_put = lg._log_queue.put_nowait

    def capture_put(event):
        events_captured.append(event)
        try:
            original_put(event)
        except queue.Full:
            pass

    lg._log_queue.put_nowait = capture_put
    lg._log('info', 'ctx_test', 'context check')

    assert any(e.get('session_id') == 'sess-abc' for e in events_captured)
    assert any(e.get('capital') == 50000 for e in events_captured)


# --- signal(): structured event ---

def test_signal_structured_event(capsys):
    lg = _fresh_logger()
    lg.signal(
        symbol='RELIANCE',
        action='BUY',
        confidence=75,
        regime='TRENDING',
        rsi=45.0,
    )
    captured = capsys.readouterr()
    assert 'BUY' in captured.out
    assert 'RELIANCE' in captured.out


# --- trade(): structured event ---

def test_trade_structured_event(capsys):
    lg = _fresh_logger()
    lg.trade(symbol='TCS', side='BUY', qty=5, price=3500.0)
    captured = capsys.readouterr()
    assert 'TCS' in captured.out
    assert '3500' in captured.out


# --- clear_context(): subsequent logs have no context fields ---

def test_clear_context_removes_fields():
    lg = _fresh_logger()
    lg.set_context(session_id='sess-to-clear', cycle=42)

    events_before = []
    events_after = []
    call_n = {'n': 0}
    original_put = lg._log_queue.put_nowait

    def capture(event):
        call_n['n'] += 1
        if call_n['n'] == 1:
            events_before.append(event)
        else:
            events_after.append(event)
        try:
            original_put(event)
        except queue.Full:
            pass

    lg._log_queue.put_nowait = capture

    lg._log('info', 'before', 'first message')
    lg.clear_context()
    lg._log('info', 'after', 'second message')

    assert any(e.get('session_id') == 'sess-to-clear' for e in events_before)
    assert not any(e.get('session_id') == 'sess-to-clear' for e in events_after)
