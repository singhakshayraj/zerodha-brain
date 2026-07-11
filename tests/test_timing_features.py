"""Timing-as-a-factor capture (TIMING_CORRELATION_PLAN Pillar 2): the timing
block folded into each decision's indicators — session phase, cycle, staleness,
concurrency. Derived without extra DB calls; never raises."""
import os
from datetime import timedelta
from unittest.mock import MagicMock, patch

with patch.dict(os.environ, {
    'SUPABASE_URL': 'https://fake.supabase.co',
    'SUPABASE_SERVICE_KEY': 'fake-key',
}):
    with patch('supabase.create_client', return_value=MagicMock()):
        import database  # noqa

from brain import TradingBrain, IST
from datetime import datetime


def _brain(executed=0, won=0, lost=0):
    b = TradingBrain.__new__(TradingBrain)
    b.session_stats = {'trades_executed': executed,
                       'winning_trades': won, 'losing_trades': lost}
    return b


def test_block_has_expected_keys():
    tf = _brain()._timing_features(3, [])
    for k in ('minutes_since_open', 'minutes_to_close', 'session_phase',
              'cycle', 'data_age_seconds', 'concurrency'):
        assert k in tf
    assert tf['cycle'] == 3


def test_concurrency_is_open_positions():
    # 5 executed, 1 win + 1 loss closed → 3 still open
    tf = _brain(executed=5, won=1, lost=1)._timing_features(1, [])
    assert tf['concurrency'] == 3


def test_data_age_from_last_candle_timestamp():
    ts = (datetime.now(IST) - timedelta(seconds=120)).isoformat()
    tf = _brain()._timing_features(1, [{'timestamp': ts}])
    assert tf['data_age_seconds'] is not None
    assert 110 < tf['data_age_seconds'] < 130   # ~120s, allow scheduling jitter


def test_data_age_none_without_candles():
    tf = _brain()._timing_features(1, [])
    assert tf['data_age_seconds'] is None


def test_data_age_none_on_bad_timestamp():
    tf = _brain()._timing_features(1, [{'timestamp': 'not-a-date'}])
    assert tf['data_age_seconds'] is None


def test_session_phase_is_a_known_bucket():
    tf = _brain()._timing_features(1, [])
    assert tf['session_phase'] in ('OPENING', 'MIDDAY', 'CLOSING')
