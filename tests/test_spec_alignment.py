"""ENGINEERING_SPEC alignment tests: config hash/immutability (REQ-004/031),
R-multiples (REQ-061), 3R daily stop + sanity checks (REQ-003/005),
deploy-freeze incident (REQ-072), SKIP reason codes (REQ-050)."""
import os
from unittest.mock import MagicMock, patch

import pytest

with patch.dict(os.environ, {
    'SUPABASE_URL': 'https://fake.supabase.co',
    'SUPABASE_SERVICE_KEY': 'fake-key',
}):
    with patch('supabase.create_client', return_value=MagicMock()):
        import database  # noqa

import config
import scheduler
import watchdog
from brain import _derive_reason_code, _r_multiple
from risk_manager import RiskManager


# --- config hash (REQ-004) ---

def test_config_hash_stable_and_order_independent():
    a = {'capital_deployed': 10000.0, 'max_trades': 10}
    b = {'max_trades': 10, 'capital_deployed': 10000.0}
    assert database.config_hash(a) == database.config_hash(b)
    assert len(database.config_hash(a)) == 12


def test_config_hash_changes_with_any_tunable():
    a = {'capital_deployed': 10000.0, 'max_trades': 10}
    b = {'capital_deployed': 10000.0, 'max_trades': 11}
    assert database.config_hash(a) != database.config_hash(b)


# --- immutable config rebuild (REQ-031) ---

def test_config_from_session_row_roundtrip():
    row = {
        'capital_deployed': 25000, 'max_trades': 10, 'max_loss_percent': 5,
        'max_profit_percent': 15, 'trade_interval_seconds': 300,
        'stock_universe': 'NIFTY50',
    }
    cfg = scheduler._config_from_session_row(row)
    assert cfg == {
        'capitalDeployed': 25000.0, 'maxTrades': 10, 'maxLossPercent': 5.0,
        'maxProfitPercent': 15.0, 'tradeIntervalSeconds': 300,
        'stockUniverse': 'NIFTY50',
    }


# --- sanity checks (REQ-005) ---

def _cfg(**over):
    c = {'capitalDeployed': 25000, 'maxLossPercent': 5,
         'maxProfitPercent': 15, 'maxTrades': 10}
    c.update(over)
    return c


def test_validate_ok_config_passes():
    assert scheduler._validate_session_config(_cfg()) == ''


def test_validate_rejects_zero_capital():
    assert 'capital' in scheduler._validate_session_config(
        _cfg(capitalDeployed=0))


def test_validate_rejects_floor_inside_daily_stop():
    # floor 2% < 3R (3 × 1%) — floor would fire before the operational stop
    err = scheduler._validate_session_config(_cfg(maxLossPercent=2))
    assert 'daily stop' in err


# --- 3R daily stop (REQ-003) ---

_SESSION_CFG = {'capitalDeployed': 10000, 'maxLossPercent': 5,
                'maxProfitPercent': 15, 'maxTrades': 25}


def _stats(pnl, unrealized=0):
    return {'total_pnl': pnl, 'trades_executed': 1, 'consecutive_losses': 0,
            'unrealized_pnl': unrealized, 'winning_trades': 0,
            'losing_trades': 1}


def test_daily_stop_fires_at_3r():
    rm = RiskManager()
    # R = 1% of 10000 = 100; 3R = 300
    with patch.object(rm, 'is_market_open', return_value=True):
        res = rm.check_session_limits(_stats(-300), _SESSION_CFG)
    assert res['can_trade'] is False
    assert res['reason'].startswith('DAILY_STOP_3R')


def test_daily_stop_fires_before_floor():
    rm = RiskManager()
    # -350 breaches 3R (-300) but not the floor (-500): 3R must be the
    # reason, proving it fires first (REQ-003)
    with patch.object(rm, 'is_market_open', return_value=True):
        res = rm.check_session_limits(_stats(-350), _SESSION_CFG)
    assert res['reason'].startswith('DAILY_STOP_3R')


def test_daily_stop_counts_open_losses():
    rm = RiskManager()
    with patch.object(rm, 'is_market_open', return_value=True):
        res = rm.check_session_limits(_stats(-200, unrealized=-150),
                                      _SESSION_CFG)
    assert res['can_trade'] is False
    assert res['reason'].startswith('DAILY_STOP_3R')


def test_inside_3r_still_trades():
    rm = RiskManager()
    with patch.object(rm, 'is_market_open', return_value=True):
        res = rm.check_session_limits(_stats(-299), _SESSION_CFG)
    assert res['can_trade'] is True


# --- R-multiple (REQ-061) ---

def test_r_multiple_long_loss_is_minus_one_at_stop():
    trade = {'entry_price': 100, 'stop_loss_price': 95, 'quantity': 10}
    assert _r_multiple(trade, -50) == -1.0


def test_r_multiple_win():
    trade = {'entry_price': 100, 'stop_loss_price': 95, 'quantity': 10}
    assert _r_multiple(trade, 100) == 2.0


def test_r_multiple_none_without_stop():
    assert _r_multiple({'entry_price': 100, 'stop_loss_price': None,
                        'quantity': 10}, 50) is None
    assert _r_multiple({'entry_price': None, 'stop_loss_price': 95,
                        'quantity': 10}, 50) is None


# --- reason codes (REQ-050) ---

def _signal(action='HOLD', skips=None):
    return {'action': action, 'skip_reasons': skips or []}


def test_reason_code_none_for_entries():
    assert _derive_reason_code(_signal('BUY')) is None
    assert _derive_reason_code(_signal('SELL')) is None


@pytest.mark.parametrize('text,code', [
    ('No BUY in CHOPPY regime', 'REGIME_CHOPPY'),
    ('Regime VOLATILE not suitable for BUY', 'REGIME_UNSUITABLE'),
    ('WEAK_TREND requires confidence >= 80%, got 72%', 'WEAK_TREND_CONFIDENCE'),
    ('R:R 1.10 below minimum 1.5', 'RR_BELOW_MIN'),
    ('Buy: 40/100, Sell: 30/100 — below thresholds', 'LOW_CONFIDENCE'),
    ('BUY blocked — Nifty in downtrend', 'NIFTY_DIRECTION_BLOCK'),
])
def test_reason_code_mappings(text, code):
    assert _derive_reason_code(_signal(skips=[text])) == code


def test_reason_code_fallback():
    assert _derive_reason_code(_signal(skips=['something novel'])) == 'HOLD_OTHER'


# --- deploy-freeze incident (REQ-072) ---

def test_watchdog_alerts_on_deploy_incident():
    import pytz
    from datetime import datetime
    ist = pytz.timezone('Asia/Kolkata')
    midday = ist.localize(datetime(2026, 7, 7, 12, 0))
    from datetime import timedelta, timezone
    ping = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()
    state = {
        'heartbeat': {'last_ping': ping, 'status': 'RUNNING', 'message': 'ok'},
        'brain_status': 'RUNNING',
        'active_session_id': 's1',
        'trades_today': 2,
        'deploy_incident': '2026-07-07T11:58:00 code changed abc -> def',
    }
    keys = [k for _, k, _ in watchdog.evaluate(state, midday)]
    assert any(k.startswith('deploy-incident-') for k in keys)


def test_session_row_hash_matches_created_hash():
    """The hash of a config rebuilt from the session row must equal the hash
    computed at creation — otherwise resume would flag a false config change."""
    session_config = {'capitalDeployed': 25000, 'maxTrades': 10,
                      'maxLossPercent': 5, 'maxProfitPercent': 15,
                      'tradeIntervalSeconds': 300, 'stockUniverse': 'NIFTY50'}
    normalized = {
        'capital_deployed': float(session_config['capitalDeployed']),
        'max_trades': int(session_config['maxTrades']),
        'max_loss_percent': float(session_config['maxLossPercent']),
        'max_profit_percent': float(session_config['maxProfitPercent']),
        'trade_interval_seconds': int(session_config['tradeIntervalSeconds']),
        'stock_universe': str(session_config['stockUniverse']),
    }
    h1 = database.config_hash(normalized)
    rebuilt = scheduler._config_from_session_row(normalized)
    renormalized = {
        'capital_deployed': float(rebuilt['capitalDeployed']),
        'max_trades': int(rebuilt['maxTrades']),
        'max_loss_percent': float(rebuilt['maxLossPercent']),
        'max_profit_percent': float(rebuilt['maxProfitPercent']),
        'trade_interval_seconds': int(rebuilt['tradeIntervalSeconds']),
        'stock_universe': str(rebuilt['stockUniverse']),
    }
    assert database.config_hash(renormalized) == h1
