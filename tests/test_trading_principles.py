"""T1.3 — TradingPrinciples unit tests."""
import pytest
from datetime import datetime
import pytz

from trading_principles import TradingPrinciples

IST = pytz.timezone('Asia/Kolkata')


# --- kelly_fraction ---

def test_kelly_fraction_normal():
    result = TradingPrinciples.kelly_fraction(0.6, 200.0, 100.0)
    assert 0.01 <= result <= 0.25


def test_kelly_fraction_clamps_to_min():
    # Very low win rate → kelly_f negative → clamp to 0.01
    result = TradingPrinciples.kelly_fraction(0.1, 10.0, 100.0)
    assert result == 0.01


def test_kelly_fraction_clamps_to_max():
    # Very high win rate → kelly_f large → clamp to 0.25
    result = TradingPrinciples.kelly_fraction(0.99, 1000.0, 1.0)
    assert result == 0.25


def test_kelly_fraction_invalid_win_rate_zero():
    result = TradingPrinciples.kelly_fraction(0.0, 200.0, 100.0)
    assert result == 0.01


def test_kelly_fraction_invalid_win_rate_above_one():
    result = TradingPrinciples.kelly_fraction(1.5, 200.0, 100.0)
    assert result == 0.01


def test_kelly_fraction_avg_loss_zero():
    result = TradingPrinciples.kelly_fraction(0.6, 200.0, 0.0)
    assert result == 0.01


def test_kelly_fraction_avg_win_zero():
    result = TradingPrinciples.kelly_fraction(0.6, 0.0, 100.0)
    assert result == 0.01


# --- is_valid_risk_reward ---

def test_rr_valid():
    result = TradingPrinciples.is_valid_risk_reward(100.0, 95.0, 112.0)
    assert result['valid'] is True
    assert result['ratio'] >= 2.0


def test_rr_below_minimum():
    result = TradingPrinciples.is_valid_risk_reward(100.0, 99.0, 101.5)
    assert result['valid'] is False


def test_rr_stop_above_entry():
    # risk <= 0 → invalid
    result = TradingPrinciples.is_valid_risk_reward(100.0, 105.0, 120.0)
    assert result['valid'] is False
    assert result['ratio'] == 0


def test_rr_target_below_entry():
    # reward <= 0 → invalid
    result = TradingPrinciples.is_valid_risk_reward(100.0, 95.0, 98.0)
    assert result['valid'] is False
    assert result['ratio'] == 0


def test_rr_exact_minimum():
    # ratio exactly 2.0
    result = TradingPrinciples.is_valid_risk_reward(100.0, 95.0, 110.0)
    assert result['valid'] is True
    assert result['ratio'] == 2.0


# --- calculate_expectancy ---

def test_expectancy_positive():
    result = TradingPrinciples.calculate_expectancy(0.6, 200.0, 100.0)
    assert result > 0


def test_expectancy_negative():
    result = TradingPrinciples.calculate_expectancy(0.3, 100.0, 200.0)
    assert result < 0


def test_expectancy_invalid_win_rate():
    result = TradingPrinciples.calculate_expectancy(0.0, 200.0, 100.0)
    assert result == -1.0


# --- should_continue_trading ---

def test_should_continue_trading_ok():
    result = TradingPrinciples.should_continue_trading(-100.0, 10000.0, 5.0, 1, 3)
    assert result['should_continue'] is True


def test_should_stop_max_loss_hit():
    result = TradingPrinciples.should_continue_trading(-500.0, 10000.0, 5.0, 0, 3)
    assert result['should_continue'] is False
    assert 'Max loss' in result['reason']


def test_should_stop_consecutive_losses():
    result = TradingPrinciples.should_continue_trading(-10.0, 10000.0, 5.0, 3, 3)
    assert result['should_continue'] is False
    assert 'consecutive' in result['reason'].lower()


def test_should_continue_with_positive_pnl():
    result = TradingPrinciples.should_continue_trading(200.0, 10000.0, 5.0, 0, 3)
    assert result['should_continue'] is True


# --- adjust_confidence_by_market ---

def test_adjust_trending_bullish_nifty():
    result = TradingPrinciples.adjust_confidence_by_market(70, 'TRENDING', 'BULLISH')
    # +5 trending + 5 bullish nifty = +10
    assert result == 80


def test_adjust_choppy_reduces_confidence():
    result = TradingPrinciples.adjust_confidence_by_market(70, 'CHOPPY', 'NEUTRAL')
    # -20 choppy -5 neutral = -25 → 45
    assert result == 45


def test_adjust_clamps_to_zero():
    result = TradingPrinciples.adjust_confidence_by_market(10, 'CHOPPY', 'NEUTRAL')
    assert result >= 0


def test_adjust_clamps_to_100():
    result = TradingPrinciples.adjust_confidence_by_market(99, 'TRENDING', 'BULLISH')
    assert result <= 100


def test_adjust_weak_trend_penalty():
    result = TradingPrinciples.adjust_confidence_by_market(70, 'WEAK_TREND', 'NEUTRAL')
    # -5 weak -5 neutral = -10 → 60
    assert result == 60


# --- is_tradeable_indian_stock ---

def test_tradeable_normal_hours():
    t = datetime(2026, 5, 21, 10, 0, tzinfo=IST)
    result = TradingPrinciples.is_tradeable_indian_stock('RELIANCE', time=t)
    assert result['tradeable'] is True


def test_not_tradeable_opening_chaos():
    t = datetime(2026, 5, 21, 9, 20, tzinfo=IST)
    result = TradingPrinciples.is_tradeable_indian_stock('RELIANCE', time=t)
    assert result['tradeable'] is False


def test_not_tradeable_closing_window():
    t = datetime(2026, 5, 21, 15, 20, tzinfo=IST)
    result = TradingPrinciples.is_tradeable_indian_stock('RELIANCE', time=t)
    assert result['tradeable'] is False


def test_not_tradeable_corporate_action():
    result = TradingPrinciples.is_tradeable_indian_stock('TCS', is_corporate_action=True)
    assert result['tradeable'] is False
