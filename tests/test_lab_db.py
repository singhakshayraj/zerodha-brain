from unittest.mock import patch

import pytest

import lab_db
from lab_book import Book
from tests.fake_supabase import FakeSupabase

ROW = {'id': 'GAPGO_s1_base_v1', 'arch': 'GAPGO', 'params': {}, 'rule': 'r', 'spec_hash': 'aaa'}


def test_register_rejects_changed_hash():
    f = FakeSupabase()
    f.tables['lab_strategies'] = [dict(ROW)]
    with patch.object(lab_db.db, 'supabase', f), pytest.raises(ValueError):
        lab_db.register([{**ROW, 'spec_hash': 'bbb'}])


def test_register_idempotent_same_hash():
    f = FakeSupabase()
    f.tables['lab_strategies'] = [dict(ROW)]
    with patch.object(lab_db.db, 'supabase', f):
        lab_db.register([dict(ROW)])
    assert not [c for c in f.calls if c[1] in ('upsert', 'insert')]


def test_register_inserts_new_ids():
    f = FakeSupabase()
    with patch.object(lab_db.db, 'supabase', f):
        lab_db.register([dict(ROW)])
    assert [r['id'] for r in f.tables['lab_strategies']] == [ROW['id']]


def test_open_close_load_round_trip():
    f = FakeSupabase()
    b = Book('T')
    p = b.try_open('X', 'LONG', 100.0, 99.0, None, 5, '2026-11-19T09:35:00+05:30')
    with patch.object(lab_db.db, 'supabase', f):
        p.db_id = lab_db.insert_open(p, '2026-11-19')
        assert [r['symbol'] for r in lab_db.load_open('2026-11-19')] == ['X']
        lab_db.close_trade(p.db_id, b.close(p, 101.0, '2026-11-19T15:15:00+05:30', 'EOD'))
        assert lab_db.load_open('2026-11-19') == []
    row = f.tables['lab_trades'][0]
    assert row['status'] == 'CLOSED' and row['exit_reason'] == 'EOD' and row['net_inr'] is not None
