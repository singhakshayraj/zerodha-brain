import pytest
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import config


def test_min_buy_confidence():
    assert config.MIN_BUY_CONFIDENCE == 70


def test_min_sell_confidence():
    assert config.MIN_SELL_CONFIDENCE == 60


def test_min_risk_reward():
    assert config.MIN_RISK_REWARD_RATIO == 2.0


def test_max_position_percent():
    assert config.MAX_POSITION_PERCENT == 0.40


def test_min_position_value():
    assert config.MIN_POSITION_VALUE == 2000


def test_max_trades_per_cycle():
    assert config.MAX_TRADES_PER_CYCLE == 3


def test_kelly_multiplier():
    assert config.KELLY_SAFETY_MULTIPLIER == 0.33


def test_adx_thresholds_sane():
    assert config.ADX_TRENDING_THRESHOLD > config.ADX_WEAK_THRESHOLD > 0


def test_no_zero_thresholds():
    assert config.MIN_BUY_CONFIDENCE > 0
    assert config.MIN_SELL_CONFIDENCE > 0
