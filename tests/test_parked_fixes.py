"""Pins for the 2026-07-13 parked-bug fixes: decision freeze after
evaluation, durable bot offset, weekly profile job."""
import os
from unittest.mock import MagicMock, patch

with patch.dict(os.environ, {
    'SUPABASE_URL': 'https://fake.supabase.co',
    'SUPABASE_SERVICE_KEY': 'fake-key',
}):
    with patch('supabase.create_client', return_value=MagicMock()):
        import database as db

import advisor_bot as bot
import data_jobs


# ── Fix 2: decision writes frozen once the row is evaluated ─────────────────

def test_decision_update_filters_unevaluated_rows_only():
    chain = MagicMock()
    chain.execute.return_value = MagicMock(data=[{'id': 1}])
    tbl = MagicMock()
    tbl.update.return_value.eq.return_value.eq.return_value \
        .is_.return_value = chain
    with patch.object(db, 'supabase') as sb:
        sb.table.return_value = tbl
        assert db.record_advice_decision('2026-07-14', 'NTPC', 'accept') is True
    # the update chain MUST scope to evaluated_at IS NULL
    tbl.update.return_value.eq.return_value.eq.return_value \
        .is_.assert_called_once_with('evaluated_at', 'null')


def test_decision_update_rejected_when_already_evaluated():
    chain = MagicMock()
    chain.execute.return_value = MagicMock(data=[])   # filter matched 0 rows
    tbl = MagicMock()
    tbl.update.return_value.eq.return_value.eq.return_value \
        .is_.return_value = chain
    with patch.object(db, 'supabase') as sb:
        sb.table.return_value = tbl
        assert db.record_advice_decision('2026-06-01', 'OLD', 'accept') is False


def test_decision_rejects_garbage_decision_value():
    assert db.record_advice_decision('2026-07-14', 'NTPC', 'buy') is False


# ── Fix 3: durable getUpdates offset ─────────────────────────────────────────

def test_offset_saved_after_poll_and_loaded_on_start():
    bot._offset = None
    upd = {'update_id': 41, 'callback_query': None}
    with patch.object(bot.telegram, 'get_updates', return_value=[upd]), \
         patch.object(bot.db, 'write_config') as wr:
        bot._poll_once()
    wr.assert_called_once_with('advisor_bot_offset', '42')

    with patch.object(bot.db, 'get_config', return_value='42'):
        bot._load_offset()
    assert bot._offset == 42
    # empty poll doesn't touch the durable marker
    with patch.object(bot.telegram, 'get_updates', return_value=[]), \
         patch.object(bot.db, 'write_config') as wr2:
        bot._poll_once()
    wr2.assert_not_called()


def test_offset_load_failure_is_safe():
    with patch.object(bot.db, 'get_config', side_effect=Exception('down')):
        bot._load_offset()
    assert bot._offset is None


# ── Fix 6: weekly profile job ────────────────────────────────────────────────

def test_weekly_profiles_once_per_iso_week():
    md = MagicMock()
    with patch.object(data_jobs.db, 'get_config', return_value='2026-W29'), \
         patch.object(data_jobs, 'build_weekly_profiles') as build, \
         patch.object(data_jobs, 'datetime') as dt:
        dt.now.return_value.strftime.return_value = '2026-W29'
        assert data_jobs.maybe_weekly_profiles(md) == 0
    build.assert_not_called()

    with patch.object(data_jobs.db, 'get_config', return_value='2026-W28'), \
         patch.object(data_jobs.db, 'write_config') as wr, \
         patch.object(data_jobs, 'build_weekly_profiles',
                      return_value=95) as build, \
         patch.object(data_jobs, 'datetime') as dt:
        dt.now.return_value.strftime.return_value = '2026-W29'
        assert data_jobs.maybe_weekly_profiles(md) == 95
    build.assert_called_once_with(md)
    wr.assert_called_once_with('profiles_week', '2026-W29')


def test_weekly_profiles_failure_never_raises():
    with patch.object(data_jobs.db, 'get_config',
                      side_effect=Exception('db down')):
        assert data_jobs.maybe_weekly_profiles(MagicMock()) == 0


def test_build_weekly_profiles_isolates_symbol_failures():
    md = MagicMock()
    md._instrument_cache = {}
    md.get_candles.side_effect = Exception('fetch fails for everyone')
    with patch.object(data_jobs.db, 'upsert_stock_profile'):
        n = data_jobs.build_weekly_profiles(md, asof='2026-07-13')
    # every fetch failed -> profiles still built from empty dailies via the
    # universe-average fallback path, and nothing raised
    assert isinstance(n, int)
