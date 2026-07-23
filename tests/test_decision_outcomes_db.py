"""database.py helpers backing Track C decision labeling (2026-07-15)."""
import os
from unittest.mock import MagicMock, patch

with patch.dict(os.environ, {
    'SUPABASE_URL': 'https://fake.supabase.co',
    'SUPABASE_SERVICE_KEY': 'fake-key',
}):
    with patch('supabase.create_client', return_value=MagicMock()):
        import database as db


def _result(data):
    m = MagicMock()
    m.data = data
    return m


def test_get_directional_decisions_excludes_already_labeled():
    # hourly pagination: one hour returns [a, b], the other 23 return empty.
    tbl = MagicMock()
    tbl.select.return_value.in_.return_value.gte.return_value.lt \
        .return_value.execute.side_effect = (
            [_result([{'id': 'a', 'symbol': 'X'}, {'id': 'b', 'symbol': 'Y'}])]
            + [_result([]) for _ in range(23)])
    outcomes_tbl = MagicMock()
    outcomes_tbl.select.return_value.in_.return_value.execute \
        .return_value.data = [{'decision_id': 'a'}]

    def table_router(name):
        return outcomes_tbl if name == 'decision_outcomes' else tbl

    with patch.object(db, 'supabase') as sb:
        sb.table.side_effect = table_router
        rows = db.get_directional_decisions_for_date('2026-07-15')
    assert [r['id'] for r in rows] == ['b']


def test_get_directional_decisions_paginates_across_hours():
    # rows in two different hours both come back (proves the loop accumulates)
    tbl = MagicMock()
    tbl.select.return_value.in_.return_value.gte.return_value.lt \
        .return_value.execute.side_effect = (
            [_result([{'id': 'a', 'symbol': 'X'}])]
            + [_result([]) for _ in range(9)]
            + [_result([{'id': 'z', 'symbol': 'Q'}])]
            + [_result([]) for _ in range(13)])
    outcomes_tbl = MagicMock()
    outcomes_tbl.select.return_value.in_.return_value.execute \
        .return_value.data = []

    def table_router(name):
        return outcomes_tbl if name == 'decision_outcomes' else tbl

    with patch.object(db, 'supabase') as sb:
        sb.table.side_effect = table_router
        rows = db.get_directional_decisions_for_date('2026-07-15')
    assert sorted(r['id'] for r in rows) == ['a', 'z']


def test_get_directional_decisions_empty_short_circuits():
    tbl = MagicMock()
    tbl.select.return_value.in_.return_value.gte.return_value.lt \
        .return_value.execute.return_value.data = []
    with patch.object(db, 'supabase') as sb:
        sb.table.return_value = tbl
        rows = db.get_directional_decisions_for_date('2026-07-15')
    assert rows == []


def test_get_directional_decisions_fails_safe_empty_on_error():
    with patch.object(db, 'supabase') as sb:
        sb.table.side_effect = RuntimeError('boom')
        assert db.get_directional_decisions_for_date('2026-07-15') == []


def test_get_candles_for_symbol_from():
    tbl = MagicMock()
    tbl.select.return_value.eq.return_value.eq.return_value.eq \
        .return_value.gte.return_value.order.return_value.execute \
        .return_value.data = [{'ts': 't', 'close': 100}]
    with patch.object(db, 'supabase') as sb:
        sb.table.return_value = tbl
        rows = db.get_candles_for_symbol_from('INFY', '2026-07-15T04:00:00Z', '2026-07-15')
    assert rows == [{'ts': 't', 'close': 100}]


def test_get_candles_for_symbol_from_fails_safe_empty():
    with patch.object(db, 'supabase') as sb:
        sb.table.side_effect = RuntimeError('boom')
        assert db.get_candles_for_symbol_from('INFY', 't', 'd') == []


def test_insert_decision_outcome():
    tbl = MagicMock()
    with patch.object(db, 'supabase') as sb:
        sb.table.return_value = tbl
        row = {'decision_id': 'a', 'symbol': 'INFY'}
        assert db.insert_decision_outcome(row) is True
    tbl.insert.assert_called_once_with(row)


def test_insert_decision_outcome_fails_safe_false():
    with patch.object(db, 'supabase') as sb:
        sb.table.side_effect = RuntimeError('boom')
        assert db.insert_decision_outcome({'decision_id': 'a'}) is False
