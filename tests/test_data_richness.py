"""Data-richness pacing (2026-07-14): trade caps gate only NEW entries —
analysis never stops — and entries spread across the day (hourly pace) and
across names (per-symbol cap). All pacing is DATA_COLLECTION_MODE-scoped;
plain paper/live behavior keeps the original caps."""
import os
from unittest.mock import MagicMock, patch

with patch.dict(os.environ, {
    'SUPABASE_URL': 'https://fake.supabase.co',
    'SUPABASE_SERVICE_KEY': 'fake-key',
}):
    with patch('supabase.create_client', return_value=MagicMock()):
        import database  # noqa

import config
from brain import TradingBrain


def _brain():
    b = TradingBrain()
    b.session_id = 'sess-t'
    b.session_stats = {'trades_executed': 0, 'total_pnl': 0}
    return b


# ── _entry_block ─────────────────────────────────────────────────────────────

def test_budget_and_cycle_gates_always_active():
    b = _brain()
    assert b._entry_block('AAA', remaining_trades=0,
                          trades_this_cycle=0) == 'DAILY_TRADE_BUDGET'
    assert b._entry_block('AAA', remaining_trades=5,
                          trades_this_cycle=config.MAX_TRADES_PER_CYCLE) \
        == 'CYCLE_LIMIT'
    assert b._entry_block('AAA', remaining_trades=5, trades_this_cycle=0) == ''


def test_symbol_cap_only_in_data_mode():
    b = _brain()
    b._symbol_trades_today['AAA'] = config.DATA_MAX_TRADES_PER_SYMBOL
    with patch.object(config, 'data_collection_active', return_value=True):
        assert b._entry_block('AAA', 5, 0) == 'SYMBOL_DAY_CAP'
        assert b._entry_block('BBB', 5, 0) == ''      # other names unaffected
    with patch.object(config, 'data_collection_active', return_value=False):
        assert b._entry_block('AAA', 5, 0) == ''      # scoped to data mode


def test_hourly_pace_only_in_data_mode():
    import brain as brain_mod
    b = _brain()
    fake_now = MagicMock()
    fake_now.hour = 10
    b._hour_trades[10] = config.DATA_MAX_NEW_TRADES_PER_HOUR
    with patch.object(brain_mod, 'datetime') as dt:
        dt.now.return_value = fake_now
        with patch.object(config, 'data_collection_active', return_value=True):
            assert b._entry_block('AAA', 5, 0) == 'HOURLY_PACE'
        with patch.object(config, 'data_collection_active', return_value=False):
            assert b._entry_block('AAA', 5, 0) == ''
    # a different hour has fresh budget
    fake_now.hour = 11
    with patch.object(brain_mod, 'datetime') as dt:
        dt.now.return_value = fake_now
        with patch.object(config, 'data_collection_active', return_value=True):
            assert b._entry_block('AAA', 5, 0) == ''


def test_note_entry_bumps_both_counters():
    import brain as brain_mod
    b = _brain()
    fake_now = MagicMock()
    fake_now.hour = 13
    with patch.object(brain_mod, 'datetime') as dt:
        dt.now.return_value = fake_now
        b._note_entry('AAA')
        b._note_entry('AAA')
        b._note_entry('BBB')
    assert b._symbol_trades_today == {'AAA': 2, 'BBB': 1}
    assert b._hour_trades == {13: 3}


# ── deferred-entry logging ───────────────────────────────────────────────────

def test_entry_deferred_logged_once_per_symbol_reason():
    b = _brain()
    with patch.object(b, '_log_activity_safe') as log:
        b._log_entry_deferred('AAA', 'BUY', 'HOURLY_PACE')
        b._log_entry_deferred('AAA', 'BUY', 'HOURLY_PACE')   # dedup
        b._log_entry_deferred('AAA', 'BUY', 'SYMBOL_DAY_CAP')  # new reason
        b._log_entry_deferred('BBB', 'SHORT', 'HOURLY_PACE')   # new symbol
    assert log.call_count == 3
    assert log.call_args_list[0].args[0] == 'ENTRY_DEFERRED'


# ── budget floor ─────────────────────────────────────────────────────────────

def test_data_mode_budget_floor_config():
    """The floor that raises a 10-trade session to a 40-entry data day.
    (Applied in run(): max(session maxTrades, DATA_MAX_TRADES_PER_DAY) when
    data collection is active — mirrored here as the config contract.)"""
    assert config.DATA_MAX_TRADES_PER_DAY >= 25
    assert config.DATA_MAX_TRADES_PER_SYMBOL >= 2
    assert config.DATA_MAX_NEW_TRADES_PER_HOUR >= 3
    assert max(10, config.DATA_MAX_TRADES_PER_DAY) == config.DATA_MAX_TRADES_PER_DAY
