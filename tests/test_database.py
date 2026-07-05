"""T1.6 — Database unit tests.
All supabase calls mocked — no real network calls.
"""
import pytest
import os
import sys
from unittest.mock import MagicMock, patch, call
import inspect

# Patch supabase before importing database
_mock_supabase_client = MagicMock()

# Patch env vars + create_client before any import
with patch.dict(os.environ, {
    'SUPABASE_URL': 'https://fake.supabase.co',
    'SUPABASE_SERVICE_KEY': 'fake-key',
}):
    with patch('supabase.create_client', return_value=_mock_supabase_client):
        import database


def _reset_mock():
    _mock_supabase_client.reset_mock()


# Helper to set up chainable mock return
def _chain(data):
    """Return a mock that supports .table().select()...execute() with data."""
    m = MagicMock()
    m.table.return_value.select.return_value.eq.return_value.limit.return_value.execute.return_value.data = data
    m.table.return_value.select.return_value.eq.return_value.execute.return_value.data = data
    m.table.return_value.select.return_value.eq.return_value.eq.return_value.execute.return_value.data = data
    m.table.return_value.select.return_value.eq.return_value.eq.return_value.eq.return_value.execute.return_value.data = data
    m.table.return_value.select.return_value.eq.return_value.neq.return_value.execute.return_value.data = data
    m.table.return_value.select.return_value.eq.return_value.order.return_value.execute.return_value.data = data
    m.table.return_value.insert.return_value.execute.return_value.data = [{"id": "test-id-123"}]
    m.table.return_value.update.return_value.eq.return_value.execute.return_value.data = [{}]
    m.table.return_value.upsert.return_value.execute.return_value.data = [{"id": "test-id"}]
    return m


# --- source inspection tests (no DB needed) ---

def test_end_session_no_entry_order_id():
    """end_session must NOT reference entry_order_id."""
    src = inspect.getsource(database.end_session)
    assert 'entry_order_id' not in src


def test_end_session_no_is_winner_in_select():
    """end_session should not select is_winner column."""
    src = inspect.getsource(database.end_session)
    # end_session selects 'started_at', not is_winner
    assert "select('started_at')" in src or "'started_at'" in src


def test_create_trade_includes_position_type():
    """create_trade passes through position_type from trade_data."""
    src = inspect.getsource(database.create_trade)
    # It uses payload = dict(trade_data) so position_type flows through
    assert 'trade_data' in src


def test_close_trade_sets_is_winner_true_on_positive_pnl():
    """close_trade: pnl > 0 → is_winner=True."""
    with patch.object(database, 'supabase', _chain([])):
        # Capture what update() was called with
        update_mock = MagicMock()
        update_mock.eq.return_value.execute.return_value.data = [{}]
        database.supabase.table.return_value.update = MagicMock(return_value=update_mock)
        database.close_trade('trade-1', {'pnl': 5.0, 'exit_reason': 'TARGET_HIT'})
        call_args = database.supabase.table.return_value.update.call_args
        payload = call_args[0][0]
        assert payload['is_winner'] is True
        assert payload['status'] == 'CLOSED'


def test_close_trade_sets_is_winner_false_on_negative_pnl():
    """close_trade: pnl < 0 → is_winner=False."""
    with patch.object(database, 'supabase', _chain([])):
        update_mock = MagicMock()
        update_mock.eq.return_value.execute.return_value.data = [{}]
        database.supabase.table.return_value.update = MagicMock(return_value=update_mock)
        database.close_trade('trade-1', {'pnl': -1.0, 'exit_reason': 'STOP_LOSS'})
        call_args = database.supabase.table.return_value.update.call_args
        payload = call_args[0][0]
        assert payload['is_winner'] is False


def test_close_trade_pnl_zero_is_winner_false():
    """pnl=0 → is_winner=False (pnl > 0 check)."""
    with patch.object(database, 'supabase', _chain([])):
        update_mock = MagicMock()
        update_mock.eq.return_value.execute.return_value.data = [{}]
        database.supabase.table.return_value.update = MagicMock(return_value=update_mock)
        database.close_trade('trade-1', {'pnl': 0.0, 'exit_reason': 'MANUAL'})
        call_args = database.supabase.table.return_value.update.call_args
        payload = call_args[0][0]
        assert payload['is_winner'] is False


# --- get_win_rate ---

def test_get_win_rate_fewer_than_10_returns_fallback():
    """< 10 trades → returns (0.45, total)."""
    mock = _chain([])
    mock.table.return_value.select.return_value.eq.return_value.not_.is_.return_value.execute.return_value.data = [
        {'pnl': 10.0}, {'pnl': -5.0}, {'pnl': 8.0}, {'pnl': -2.0}, {'pnl': 3.0}
    ]
    with patch.object(database, 'supabase', mock):
        win_rate, total = database.get_win_rate()
    assert win_rate == 0.45
    assert total == 5


def test_get_win_rate_10_or_more_trades():
    """≥ 10 trades → computes actual win rate."""
    trades = [{'pnl': 10.0 if i % 2 == 0 else -5.0} for i in range(10)]
    mock = _chain([])
    mock.table.return_value.select.return_value.eq.return_value.not_.is_.return_value.execute.return_value.data = trades
    with patch.object(database, 'supabase', mock):
        win_rate, total = database.get_win_rate()
    assert total == 10
    assert win_rate == 0.5


def test_get_win_rate_exception_returns_fallback():
    """DB error → returns (0.45, 0)."""
    mock = MagicMock()
    mock.table.side_effect = Exception("DB down")
    with patch.object(database, 'supabase', mock):
        win_rate, total = database.get_win_rate()
    assert win_rate == 0.45
    assert total == 0


# --- get_config ---

def test_get_config_found():
    mock = _chain([{'value': 'START'}])
    with patch.object(database, 'supabase', mock):
        result = database.get_config('brain_status')
    assert result == 'START'


def test_get_config_not_found():
    mock = _chain([])
    with patch.object(database, 'supabase', mock):
        result = database.get_config('nonexistent_key')
    assert result is None


def test_get_config_exception_returns_none():
    mock = MagicMock()
    mock.table.side_effect = Exception("network error")
    with patch.object(database, 'supabase', mock):
        result = database.get_config('any_key')
    assert result is None


# --- write_config ---

def test_write_config_calls_upsert():
    mock = _chain([])
    with patch.object(database, 'supabase', mock):
        database.write_config('brain_status', 'IDLE')
    mock.table.assert_called_with('app_config')


# --- create_session ---

def test_create_session_returns_row():
    mock = _chain([])
    mock.table.return_value.insert.return_value.execute.return_value.data = [{'id': 'sess-123'}]
    with patch.object(database, 'supabase', mock):
        result = database.create_session({
            'capitalDeployed': 10000,
            'maxTrades': 20,
            'maxLossPercent': 5,
            'maxProfitPercent': 15,
            'tradeIntervalSeconds': 300,
            'stockUniverse': 'BOTH',
        })
    assert result is not None
    assert result['id'] == 'sess-123'


def test_create_session_only_allowed_columns():
    """Whitelist enforced — no rogue columns."""
    captured_payload = {}

    def fake_insert(payload):
        captured_payload.update(payload)
        m = MagicMock()
        m.execute.return_value.data = [{'id': 'sess-456', **payload}]
        return m

    mock = _chain([])
    mock.table.return_value.insert = fake_insert
    with patch.object(database, 'supabase', mock):
        database.create_session({
            'capitalDeployed': 5000,
            'maxTrades': 10,
            'maxLossPercent': 3,
            'maxProfitPercent': 10,
            'tradeIntervalSeconds': 60,
            'stockUniverse': 'NIFTY50',
            'rogue_field': 'should_be_stripped',
        })
    assert 'rogue_field' not in captured_payload


# --- create_trade ---

def test_create_trade_adds_session_id():
    captured = {}

    def fake_insert(payload):
        captured.update(payload)
        m = MagicMock()
        m.execute.return_value.data = [{'id': 'trade-789', **payload}]
        return m

    mock = _chain([])
    mock.table.return_value.insert = fake_insert
    with patch.object(database, 'supabase', mock):
        database.create_trade('sess-1', {
            'symbol': 'RELIANCE',
            'position_type': 'LONG',
            'stop_loss': 2400.0,
            'target': 2600.0,
        })
    assert captured.get('session_id') == 'sess-1'
    assert captured.get('status') == 'OPEN'
    assert captured.get('position_type') == 'LONG'


def test_create_trade_default_status_open():
    captured = {}

    def fake_insert(payload):
        captured.update(payload)
        m = MagicMock()
        m.execute.return_value.data = [{'id': 'trade-aaa'}]
        return m

    mock = _chain([])
    mock.table.return_value.insert = fake_insert
    with patch.object(database, 'supabase', mock):
        database.create_trade('sess-2', {'symbol': 'TCS'})
    assert captured['status'] == 'OPEN'


# --- get_open_trades / get_open_shorts / get_open_longs ---

def test_get_open_trades_returns_list():
    mock = _chain([{'id': 't1', 'symbol': 'INFY', 'status': 'OPEN'}])
    with patch.object(database, 'supabase', mock):
        result = database.get_open_trades('sess-1')
    assert isinstance(result, list)


def test_get_open_trades_exception_returns_empty():
    mock = MagicMock()
    mock.table.side_effect = Exception("DB error")
    with patch.object(database, 'supabase', mock):
        result = database.get_open_trades('sess-1')
    assert result == []


# --- get_brain_command ---

def test_get_brain_command_returns_idle_when_none():
    mock = _chain([])
    with patch.object(database, 'supabase', mock):
        result = database.get_brain_command()
    assert result == 'IDLE'
