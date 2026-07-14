"""portfolio_advice DB layer (2026-07-14 intraday-refresh rewrite):
write_official_portfolio_advice (delete-then-insert, replaces the old
upsert now that (run_date, symbol) is no longer unique),
insert_portfolio_advice_snapshot (plain append), and the dedup/interval
lookups the scheduler uses instead of an in-memory flag."""
import os
from unittest.mock import MagicMock, patch

with patch.dict(os.environ, {
    'SUPABASE_URL': 'https://fake.supabase.co',
    'SUPABASE_SERVICE_KEY': 'fake-key',
}):
    with patch('supabase.create_client', return_value=MagicMock()):
        import database as db


def test_write_official_deletes_prior_official_batch_then_inserts():
    tbl = MagicMock()
    with patch.object(db, 'supabase') as sb:
        sb.table.return_value = tbl
        rows = [{'run_date': '2026-07-14', 'symbol': 'INFY', 'is_official': True}]
        n = db.write_official_portfolio_advice(rows)
    assert n == 1
    tbl.delete.return_value.eq.return_value.eq.assert_called_once_with('is_official', True)
    tbl.insert.assert_called_once_with(rows)


def test_write_official_empty_rows_noop():
    with patch.object(db, 'supabase') as sb:
        assert db.write_official_portfolio_advice([]) == 0
        sb.table.assert_not_called()


def test_write_official_survives_error():
    with patch.object(db, 'supabase') as sb:
        sb.table.side_effect = RuntimeError('boom')
        assert db.write_official_portfolio_advice(
            [{'run_date': '2026-07-14', 'symbol': 'X'}]) == 0


def test_insert_snapshot_plain_append_no_delete():
    tbl = MagicMock()
    with patch.object(db, 'supabase') as sb:
        sb.table.return_value = tbl
        rows = [{'run_date': '2026-07-14', 'symbol': 'INFY', 'is_official': False}]
        n = db.insert_portfolio_advice_snapshot(rows)
    assert n == 1
    tbl.delete.assert_not_called()
    tbl.insert.assert_called_once_with(rows)


def test_has_official_advisor_run_true_when_row_exists():
    tbl = MagicMock()
    tbl.select.return_value.eq.return_value.eq.return_value.limit \
        .return_value.execute.return_value.data = [{'run_date': '2026-07-14'}]
    with patch.object(db, 'supabase') as sb:
        sb.table.return_value = tbl
        assert db.has_official_advisor_run('2026-07-14') is True


def test_has_official_advisor_run_false_when_no_row():
    tbl = MagicMock()
    tbl.select.return_value.eq.return_value.eq.return_value.limit \
        .return_value.execute.return_value.data = []
    with patch.object(db, 'supabase') as sb:
        sb.table.return_value = tbl
        assert db.has_official_advisor_run('2026-07-14') is False


def test_has_official_advisor_run_fails_safe_false_on_error():
    with patch.object(db, 'supabase') as sb:
        sb.table.side_effect = RuntimeError('boom')
        assert db.has_official_advisor_run('2026-07-14') is False


def test_get_last_advisor_run_time_returns_latest_created_at():
    tbl = MagicMock()
    tbl.select.return_value.eq.return_value.order.return_value.limit \
        .return_value.execute.return_value.data = [
            {'created_at': '2026-07-14T10:05:00+05:30'}]
    with patch.object(db, 'supabase') as sb:
        sb.table.return_value = tbl
        assert db.get_last_advisor_run_time('2026-07-14') \
            == '2026-07-14T10:05:00+05:30'


def test_get_last_advisor_run_time_none_when_nothing_ran():
    tbl = MagicMock()
    tbl.select.return_value.eq.return_value.order.return_value.limit \
        .return_value.execute.return_value.data = []
    with patch.object(db, 'supabase') as sb:
        sb.table.return_value = tbl
        assert db.get_last_advisor_run_time('2026-07-14') is None


def test_get_official_advice_for_date():
    tbl = MagicMock()
    tbl.select.return_value.eq.return_value.eq.return_value.execute \
        .return_value.data = [{'symbol': 'INFY', 'rotation_target_symbol': 'TCS'}]
    with patch.object(db, 'supabase') as sb:
        sb.table.return_value = tbl
        rows = db.get_official_advice_for_date('2026-07-14')
    assert rows == [{'symbol': 'INFY', 'rotation_target_symbol': 'TCS'}]


# ── Regression pins for the scoping fixes on existing helpers ──────────────

def test_get_unevaluated_advice_scopes_to_official_only():
    tbl = MagicMock()
    with patch.object(db, 'supabase') as sb:
        sb.table.return_value = tbl
        db.get_unevaluated_advice('2026-07-14')
    tbl.select.return_value.is_.return_value.eq \
        .assert_called_once_with('is_official', True)


def test_update_advice_outcome_scopes_to_official_only():
    tbl = MagicMock()
    with patch.object(db, 'supabase') as sb:
        sb.table.return_value = tbl
        db.update_advice_outcome('2026-07-14', 'INFY', {'outcome_correct': True})
    tbl.update.return_value.eq.return_value.eq.return_value.eq \
        .assert_called_once_with('is_official', True)
