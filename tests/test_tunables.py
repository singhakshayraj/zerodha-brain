"""Live-tunable signal knobs (REQ-030): DB-backed overrides for a whitelisted
set of signal thresholds, cached with a TTL, failing safe to compiled defaults."""
import os
from unittest.mock import MagicMock, patch

with patch.dict(os.environ, {
    'SUPABASE_URL': 'https://fake.supabase.co',
    'SUPABASE_SERVICE_KEY': 'fake-key',
}):
    with patch('supabase.create_client', return_value=MagicMock()):
        import database  # noqa

import config


def _reset_cache():
    config._tunable_cache = {}
    config._tunable_cache_ts = 0.0


def test_default_when_no_override():
    _reset_cache()
    with patch('database.get_config', return_value=None):
        assert config.get_tunable('MIN_BUY_CONFIDENCE') == config.MIN_BUY_CONFIDENCE


def test_override_applied_and_typed():
    _reset_cache()
    # JSON values arrive as strings/numbers → coerced to the default's type
    with patch('database.get_config', return_value='{"MIN_BUY_CONFIDENCE": 80}'):
        val = config.get_tunable('MIN_BUY_CONFIDENCE')
        assert val == 80
        assert isinstance(val, int)


def test_float_override_keeps_float_type():
    _reset_cache()
    with patch('database.get_config', return_value='{"MIN_RISK_REWARD_RATIO": "1.5"}'):
        val = config.get_tunable('MIN_RISK_REWARD_RATIO')
        assert val == 1.5
        assert isinstance(val, float)


def test_bad_override_falls_back_to_default():
    _reset_cache()
    with patch('database.get_config', return_value='{"MIN_BUY_CONFIDENCE": "abc"}'):
        assert config.get_tunable('MIN_BUY_CONFIDENCE') == config.MIN_BUY_CONFIDENCE


def test_db_error_falls_back_to_default_no_raise():
    _reset_cache()
    with patch('database.get_config', side_effect=RuntimeError('supabase down')):
        assert config.get_tunable('ADX_TRENDING_THRESHOLD') == config.ADX_TRENDING_THRESHOLD


def test_malformed_json_falls_back():
    _reset_cache()
    with patch('database.get_config', return_value='not json{'):
        assert config.get_tunable('MIN_SELL_CONFIDENCE') == config.MIN_SELL_CONFIDENCE


def test_cache_avoids_repeat_db_reads_within_ttl():
    _reset_cache()
    with patch('database.get_config', return_value='{"MIN_BUY_CONFIDENCE": 90}') as gc:
        config.get_tunable('MIN_BUY_CONFIDENCE')
        config.get_tunable('MIN_SELL_CONFIDENCE')
        config.get_tunable('MIN_BUY_CONFIDENCE')
        assert gc.call_count == 1   # one DB read for the whole TTL window


def test_non_whitelisted_key_raises():
    _reset_cache()
    with patch('database.get_config', return_value=None):
        # risk-sizing knobs are deliberately NOT tunable — not in the whitelist
        import pytest
        with pytest.raises(KeyError):
            config.get_tunable('RISK_PER_TRADE_PCT')
