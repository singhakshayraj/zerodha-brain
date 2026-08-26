"""Pins what reaches brain_decisions.indicators. [P-38]

That payload is 1,848 bytes/row -- over half of all projected storage growth --
and it is ALSO the substrate [P-35]'s entry-edge study runs on. So it needs a
test that fails in BOTH directions: if a redundant key comes back, and if a
load-bearing key disappears.

log_decision() merges **kwargs into the jsonb, so filtering there covers every
call site including ones that do not exist yet.
"""
import os
from unittest.mock import MagicMock, patch

# database imports db_records, which imports database back -- so db_records
# cannot be imported first. Same guarded-import pattern the other db tests use.
with patch.dict(os.environ, {
    'SUPABASE_URL': 'https://fake.supabase.co',
    'SUPABASE_SERVICE_KEY': 'fake-key',
}):
    with patch('supabase.create_client', return_value=MagicMock()):
        import database as db          # noqa: F401  (pulls db_records in)
        import db_records


def _logged_indicators(indicators=None, **kwargs):
    """Run log_decision and return the indicators dict it would have written."""
    captured = {}

    class _Tbl:
        def insert(self, payload):
            captured.update(payload)
            return self

        def execute(self):
            return type('R', (), {'data': [{'id': 'x'}]})()

    fake = MagicMock()
    fake.table.return_value = _Tbl()
    with patch.object(db_records.database, 'supabase', fake):
        db_records.log_decision(
            session_id='s', symbol='INFY', signal='BUY', confidence=70,
            indicators=indicators if indicators is not None else {'adx': 25.0},
            reasons=[], skip_reasons=[], **kwargs)
    return captured.get('indicators', {})


def test_session_constants_are_not_written():
    """git_sha and config_hash are columns on trading_sessions and every
    decision carries session_id, so embedding them repeats one value ~1,650x a
    day. Verified 2026-08-10: 1 distinct value each across 1,653 rows."""
    ind = _logged_indicators(git_sha='abc123', config_hash='def456')
    assert 'git_sha' not in ind
    assert 'config_hash' not in ind


def test_load_bearing_keys_survive():
    """[P-35] reads these. Losing one silently breaks the edge study."""
    ind = _logged_indicators(regime='TRENDING', market_bias='BULLISH',
                             stop_loss=95.0, git_sha='abc123')
    assert ind['adx'] == 25.0
    assert ind['regime'] == 'TRENDING'
    assert ind['market_bias'] == 'BULLISH'
    assert ind['stop_loss'] == 95.0


def test_event_policy_dropped_when_normal():
    """NORMAL on nearly every day; storing it costs 89 bytes a row to record
    'nothing special happened'."""
    assert 'event_policy' not in _logged_indicators(event_policy='NORMAL')


def test_event_policy_kept_when_it_is_not_normal():
    """Expiry and results days are the whole reason this field exists."""
    for policy in ('STAND_ASIDE', 'RAISE_BAR'):
        assert _logged_indicators(event_policy=policy)['event_policy'] == policy


def test_event_policy_dropped_when_normal_DICT():
    """The shape production actually writes.

    These tests previously only passed the STRING 'NORMAL', while brain.py
    writes a DICT. A dict never equals a string, so the sparse-drop never once
    fired and the test still passed -- 16,229 of 22,463 rows carrying the key
    were fully default, ~1.5 MB spent recording that nothing happened.
    """
    ind = _logged_indicators(event_policy={
        'policy': 'NORMAL', 'reasons': [],
        'weekly_expiry': False, 'monthly_expiry': False})
    assert 'event_policy' not in ind


def test_event_policy_dict_kept_when_anything_is_set():
    """Expiry and results days are the whole reason the field exists."""
    for ep in (
        {'policy': 'NORMAL', 'reasons': ['weekly expiry'],
         'weekly_expiry': False, 'monthly_expiry': False},
        {'policy': 'NORMAL', 'reasons': [],
         'weekly_expiry': True, 'monthly_expiry': False},
        {'policy': 'NORMAL', 'reasons': [],
         'weekly_expiry': False, 'monthly_expiry': True},
        {'policy': 'STAND_ASIDE', 'reasons': [],
         'weekly_expiry': False, 'monthly_expiry': False},
    ):
        assert _logged_indicators(event_policy=ep)['event_policy'] == ep


def test_unknown_event_policy_key_blocks_the_drop():
    """If the dict grows a field, keep the row rather than silently discard
    information we have not taught the predicate about yet."""
    ep = {'policy': 'NORMAL', 'reasons': [], 'weekly_expiry': False,
          'monthly_expiry': False, 'circuit_halt': True}
    assert _logged_indicators(event_policy=ep)['event_policy'] == ep


def test_indicators_argument_is_filtered_too():
    """The denylist must apply to the `indicators` argument, not just **kwargs
    -- a caller could pass a redundant key either way."""
    ind = _logged_indicators(
        indicators={'adx': 25.0, 'git_sha': 'zzz', 'event_policy': 'NORMAL'})
    assert 'git_sha' not in ind
    assert 'event_policy' not in ind
    assert ind['adx'] == 25.0
