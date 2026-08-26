"""get_win_rate must count server-side, so the book can outgrow 1000 trades.

It used to select every closed trade's pnl and count in Python. PostgREST caps
a rowset at 1000, so at 914 closed trades and ~69 a session the next session
or two would have silently returned the OLDEST 1000 and frozen the win rate
there permanently -- no error, just a number that stops moving. Same defect
class as [P-36]/[P-37].

The first test is the one that matters: a 5000-trade book must report 5000.
"""
import os
from unittest.mock import MagicMock, patch

with patch.dict(os.environ, {
    'SUPABASE_URL': 'https://fake.supabase.co',
    'SUPABASE_SERVICE_KEY': 'fake-key',
}):
    with patch('supabase.create_client', return_value=MagicMock()):
        import database


class _Res:
    def __init__(self, count):
        self.count = count
        self.data = []


class _Query:
    """Chainable stub. Records how select() was called and hands back the
    next queued result, so the test can prove no rows were requested."""

    def __init__(self, results, calls):
        self._results = results
        self._calls = calls

    def select(self, *a, **kw):
        self._calls.append(kw)
        return self

    def eq(self, *a, **kw):
        return self

    def gt(self, *a, **kw):
        return self

    def is_(self, *a, **kw):
        return self

    @property
    def not_(self):
        return self

    def limit(self, *a, **kw):
        return self

    def execute(self):
        return self._results.pop(0)


def _client(results, calls):
    c = MagicMock()
    c.table.return_value = _Query(results, calls)
    return c


def test_book_larger_than_1000_reports_the_truth():
    """The regression: 1000 is PostgREST's page cap, not the book's size."""
    calls = []
    with patch.object(database, 'supabase',
                      _client([_Res(5000), _Res(1200)], calls)):
        rate, total = database.get_win_rate()
    assert total == 5000, 'total must not be capped at the 1000-row page limit'
    assert abs(rate - 0.24) < 1e-9


def test_counts_are_requested_not_rows():
    calls = []
    with patch.object(database, 'supabase',
                      _client([_Res(2000), _Res(500)], calls)):
        database.get_win_rate()
    assert calls and all(c.get('count') == 'exact' for c in calls), \
        'must ask Postgres to count, never page the rows back'


def test_small_book_falls_back():
    with patch.object(database, 'supabase', _client([_Res(8)], [])):
        assert database.get_win_rate() == (0.45, 8)


def test_errors_fall_back_safely():
    broken = MagicMock()
    broken.table.side_effect = RuntimeError('supabase down')
    with patch.object(database, 'supabase', broken):
        assert database.get_win_rate() == (0.45, 0)
