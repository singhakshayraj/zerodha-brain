"""Edge + defensive-branch coverage for trading_principles.py — the live
static helpers (kelly, risk_reward, expectancy, drawdown, should_continue,
adjust_confidence, is_tradeable) must degrade to safe defaults, never raise."""
from datetime import datetime

import pytz

from trading_principles import TradingPrinciples as TP

IST = pytz.timezone('Asia/Kolkata')


def test_kelly_guards_and_defensive_except():
    assert TP.kelly_fraction(0.0, 200, 100) == 0.01     # win_rate <= 0
    assert TP.kelly_fraction(1.5, 200, 100) == 0.01     # win_rate > 1
    assert TP.kelly_fraction(0.6, 0, 100) == 0.01       # avg_win <= 0
    assert TP.kelly_fraction(0.6, 200, 0) == 0.01       # avg_loss <= 0
    assert TP.kelly_fraction(0.6, 'x', 100) == 0.01     # type -> except


def test_risk_reward_valid_invalid_and_except():
    ok = TP.is_valid_risk_reward(100, 95, 115)
    assert ok['valid'] is True and ok['ratio'] >= 2
    bad = TP.is_valid_risk_reward(100, 95, 102)
    assert bad['valid'] is False
    assert TP.is_valid_risk_reward(100, 105, 115)['valid'] is False  # risk<=0
    err = TP.is_valid_risk_reward(None, 95, 115)
    assert err['valid'] is False                        # except path


def test_expectancy_and_except():
    e = TP.calculate_expectancy(win_rate=0.6, avg_win=200, avg_loss=100)
    assert isinstance(e, float)
    assert TP.calculate_expectancy(win_rate='x', avg_win=1, avg_loss=1) == -1.0


def test_max_drawdown():
    assert TP.calculate_max_drawdown_capital(10000, 15) == 1500
    assert TP.calculate_max_drawdown_capital(10000) == 1500


def test_should_continue_trading_paths():
    # healthy
    r = TP.should_continue_trading(current_session_pnl=100,
                                   session_capital=10000)
    assert r['should_continue'] is True
    # max loss breached
    r = TP.should_continue_trading(current_session_pnl=-600,
                                   session_capital=10000, max_loss_percent=5)
    assert r['should_continue'] is False
    # consecutive-loss circuit breaker
    r = TP.should_continue_trading(current_session_pnl=0,
                                   session_capital=10000,
                                   consecutive_losses=3,
                                   max_consecutive_losses=3)
    assert r['should_continue'] is False
    # defensive except
    r = TP.should_continue_trading(current_session_pnl='x',
                                   session_capital=10000)
    assert 'should_continue' in r


def test_adjust_confidence_all_regimes_and_directions():
    for regime in ('CHOPPY', 'SIDEWAYS', 'WEAK_TREND', 'WEAK', 'TRENDING',
                   'STRONG', 'UNKNOWN'):
        for direction in ('BULLISH', 'BEARISH', 'NEUTRAL'):
            out = TP.adjust_confidence_by_market(70, regime, direction)
            assert 0 <= out <= 100
    # clamps
    assert TP.adjust_confidence_by_market(5, 'SIDEWAYS', 'NEUTRAL') == 0
    # defensive except (base_confidence returned)
    assert TP.adjust_confidence_by_market(None, 'CHOPPY', 'NEUTRAL') is None


def test_is_tradeable_windows_and_except():
    corp = TP.is_tradeable_indian_stock('X', is_corporate_action=True)
    assert corp['tradeable'] is False
    opening = TP.is_tradeable_indian_stock(
        'X', time=IST.localize(datetime(2026, 7, 14, 9, 20)))
    assert opening['tradeable'] is False
    closing = TP.is_tradeable_indian_stock(
        'X', time=IST.localize(datetime(2026, 7, 14, 15, 20)))
    assert closing['tradeable'] is False
    ok = TP.is_tradeable_indian_stock(
        'X', time=IST.localize(datetime(2026, 7, 14, 11, 0)))
    assert ok['tradeable'] is True
    # naive datetime gets localized
    naive = TP.is_tradeable_indian_stock('X', time=datetime(2026, 7, 14, 11, 0))
    assert naive['tradeable'] is True
    # defensive except: a bad 'time' type
    bad = TP.is_tradeable_indian_stock('X', time='not-a-datetime')
    assert bad['tradeable'] is True                     # fail-open
