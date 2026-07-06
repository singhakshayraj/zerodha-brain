"""Chaos-drill unit coverage (REQ-083): token expiry mid-session propagates
to a clean session end + durable incident; sustained data faults trip the
error budget; transient faults are survived. Plus watchdog alert tiers."""
import os
from unittest.mock import MagicMock, patch

import pytest

with patch.dict(os.environ, {
    'SUPABASE_URL': 'https://fake.supabase.co',
    'SUPABASE_SERVICE_KEY': 'fake-key',
}):
    with patch('supabase.create_client', return_value=MagicMock()):
        import database  # noqa

import watchdog
from kite_client import KiteAPIError, TokenExpiredError
from market_data import MarketData


# --- market_data must PROPAGATE TokenExpiredError, not swallow it ---

def _md_with_kite(kite):
    md = MarketData(kite)
    md._instrument_cache['NSE:INFY'] = 12345
    return md


def test_get_candles_propagates_token_expiry():
    kite = MagicMock()
    kite._get.side_effect = TokenExpiredError("expired")
    md = _md_with_kite(kite)
    with pytest.raises(TokenExpiredError):
        md.get_candles('NSE:INFY', '15minute')


def test_get_candles_still_swallows_other_errors():
    kite = MagicMock()
    kite._get.side_effect = KiteAPIError("500")
    md = _md_with_kite(kite)
    # non-token errors still degrade gracefully to cached/empty
    assert md.get_candles('NSE:INFY', '15minute') == []


def test_refresh_holdings_propagates_token_expiry():
    kite = MagicMock()
    kite.get_holdings.side_effect = TokenExpiredError("expired")
    md = MarketData(kite)
    with pytest.raises(TokenExpiredError):
        md.refresh_holdings_cache()


# --- FakeKiteClient fault injection ---

def _fake(fault=None):
    env = {'QA_FAULT': fault} if fault else {}
    with patch.dict(os.environ, env, clear=False):
        if not fault:
            os.environ.pop('QA_FAULT', None)
        from qa_market import FakeKiteClient
        return FakeKiteClient()


def test_fault_token_expiry_fires_after_n():
    fk = _fake('token_expiry@2')
    fk.get_ltp(['NSE:INFY'])          # fetch 1 — ok
    with pytest.raises(TokenExpiredError):
        fk.get_ltp(['NSE:INFY'])      # fetch 2 — fault


def test_fault_network_drop_is_transient():
    fk = _fake('network_drop@1')
    # first 3 fetches from the fault point raise, then it recovers
    for _ in range(3):
        with pytest.raises(KiteAPIError):
            fk.get_ltp(['NSE:INFY'])
    assert fk.get_ltp(['NSE:INFY'])   # recovered


def test_fault_network_drop_hard_is_sustained():
    fk = _fake('network_drop_hard@1')
    for _ in range(5):
        with pytest.raises(KiteAPIError):
            fk.get_ltp(['NSE:INFY'])


def test_no_fault_by_default():
    fk = _fake(None)
    for _ in range(10):
        assert fk.get_ltp(['NSE:INFY'])


# --- brain cycle-error feeds the error budget (sustained fault → DEGRADED) ---

def test_cycle_exception_records_failure():
    import brain as brain_mod
    b = brain_mod.TradingBrain.__new__(brain_mod.TradingBrain)
    b.cycle_count = 0
    b._cycle_lock = __import__('threading').Lock()
    b.universe = {}
    b.market_data = MagicMock()
    b.market_data.refresh_holdings_cache.side_effect = RuntimeError("net down")
    b.session_id = 's1'

    database._record_success()  # reset budget
    with patch('brain.db._record_failure') as rec_fail, \
         patch('brain.logger'):
        b.run_cycle()
    rec_fail.assert_called_once()


# --- watchdog tiers (REQ-071) ---

def test_token_incident_is_p1():
    import pytz
    from datetime import datetime
    ist = pytz.timezone('Asia/Kolkata')
    # outside market hours on purpose — incident flags alert anytime
    t = ist.localize(datetime(2026, 7, 7, 18, 0))
    state = {'heartbeat': None, 'brain_status': 'IDLE',
             'active_session_id': None, 'trades_today': None,
             'deploy_incident': None,
             'token_incident': '2026-07-07T14:00 token expired mid-session'}
    tiers = {k: tier for tier, k, _ in watchdog.evaluate(state, t)}
    assert any(k.startswith('token-incident-') for k in tiers)
    for k, tier in tiers.items():
        if k.startswith('token-incident-'):
            assert tier == watchdog.P1


def test_p3_alert_never_hits_telegram(monkeypatch):
    watchdog._last_sent.clear()
    monkeypatch.setattr(watchdog, 'TELEGRAM_BOT_TOKEN', 'x')
    monkeypatch.setattr(watchdog, 'TELEGRAM_CHAT_ID', 'y')
    posted = []
    monkeypatch.setattr(watchdog.requests, 'post',
                        lambda *a, **k: posted.append(1) or MagicMock())
    watchdog.send_alert('info-1', 'hi', now_ts=1.0, tier=watchdog.P3)
    assert posted == []


def test_p1_alert_hits_telegram(monkeypatch):
    watchdog._last_sent.clear()
    monkeypatch.setattr(watchdog, 'TELEGRAM_BOT_TOKEN', 'x')
    monkeypatch.setattr(watchdog, 'TELEGRAM_CHAT_ID', 'y')
    posted = []

    class _Resp:
        def raise_for_status(self):
            pass

    monkeypatch.setattr(watchdog.requests, 'post',
                        lambda *a, **k: posted.append(k) or _Resp())
    watchdog.send_alert('crit-1', 'down', now_ts=1.0, tier=watchdog.P1)
    assert len(posted) == 1
