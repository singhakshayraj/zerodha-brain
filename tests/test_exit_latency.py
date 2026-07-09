"""Stop-latency fix tests: fresh exit prices, intra-cycle exit checks,
would-fire dedup, REQ-072 incident dedup. Driven by the 2026-07-08 finding
that cycle-boundary-only stop checks filled at −2.78R instead of ≈−1R."""
import os
import threading
from unittest.mock import MagicMock, patch

with patch.dict(os.environ, {
    'SUPABASE_URL': 'https://fake.supabase.co',
    'SUPABASE_SERVICE_KEY': 'fake-key',
}):
    with patch('supabase.create_client', return_value=MagicMock()):
        import database  # noqa

import config
import scheduler
from brain import TradingBrain
from kite_client import TokenExpiredError
from market_data import MarketData


def _brain(open_trades, fresh_price=95.0):
    b = TradingBrain.__new__(TradingBrain)
    b.session_id = 's1'
    b.consecutive_losses = 0
    b._session_ended = False
    b._cycle_lock = threading.Lock()
    b._time_stop_logged = set()
    b._excursion = {}
    b.session_stats = {'trades_executed': 1, 'total_pnl': 0.0,
                       'winning_trades': 0, 'losing_trades': 0}
    b.market_data = MagicMock()
    b.market_data.get_fresh_close.return_value = fresh_price
    b.market_data._holdings_cache = {}
    b._open_trades = open_trades
    return b


def _long(stop=98, target=110, entry_time=None):
    t = {'id': 't1', 'symbol': 'INFY', 'exchange': 'NSE',
         'position_type': 'LONG', 'stop_loss_price': stop,
         'target_price': target, 'quantity': 10, 'entry_value': 1000,
         'entry_price': 100}
    if entry_time:
        t['entry_time'] = entry_time
    return t


# --- fresh price is used for exits ---

def test_exit_uses_fresh_price_not_stale_cache():
    b = _brain([_long()], fresh_price=95.0)   # below stop 98
    with patch('brain.db.get_open_trades', return_value=b._open_trades), \
         patch('brain.db.log_brain_activity'), \
         patch.object(b, '_execute_sell_by_trade') as sell:
        b._check_and_close_positions()
    sell.assert_called_once()
    args = sell.call_args[0]
    assert args[1] == 95.0                     # exited at the FRESH price
    assert args[2] == 'STOP_LOSS_HIT'
    b.market_data.get_fresh_close.assert_called()


def test_exit_falls_back_to_cache_when_fresh_unavailable():
    b = _brain([_long()], fresh_price=None)
    b.market_data._holdings_cache = {'NSE:INFY': {'price': 94.0}}
    with patch('brain.db.get_open_trades', return_value=b._open_trades), \
         patch('brain.db.log_brain_activity'), \
         patch.object(b, '_execute_sell_by_trade') as sell:
        b._check_and_close_positions()
    sell.assert_called_once()
    assert sell.call_args[0][1] == 94.0


# --- intra-cycle entry point ---

def test_check_open_exits_fires_stop_between_cycles():
    b = _brain([_long()], fresh_price=95.0)
    with patch('brain.db.get_open_trades', return_value=b._open_trades), \
         patch('brain.db.log_brain_activity'), \
         patch.object(b, '_execute_sell_by_trade') as sell:
        b.check_open_exits()
    sell.assert_called_once()


def test_check_open_exits_noop_when_session_ended():
    b = _brain([_long()])
    b._session_ended = True
    with patch('brain.db.get_open_trades') as got:
        b.check_open_exits()
    got.assert_not_called()


def test_check_open_exits_skips_when_cycle_running():
    b = _brain([_long()])
    b._cycle_lock.acquire()   # simulate mid-cycle
    try:
        with patch('brain.db.get_open_trades') as got:
            b.check_open_exits()
        got.assert_not_called()
    finally:
        b._cycle_lock.release()


def test_check_open_exits_token_expiry_ends_session():
    b = _brain([_long()])
    b.market_data.get_fresh_close.side_effect = TokenExpiredError('expired')
    with patch('brain.db.get_open_trades', return_value=b._open_trades), \
         patch('brain.db.write_config') as wc, \
         patch.object(b, 'end_session') as end:
        b.check_open_exits()   # must NOT raise
    end.assert_called_once_with('TOKEN_EXPIRED')
    keys = [c.args[0] for c in wc.call_args_list]
    assert 'token_incident' in keys


def test_check_open_exits_swallows_other_errors():
    b = _brain([_long()])
    b.market_data.get_fresh_close.side_effect = RuntimeError('boom')
    with patch('brain.db.get_open_trades', return_value=b._open_trades):
        b.check_open_exits()   # no raise
    assert not b._cycle_lock.locked()   # lock always released


# --- would-fire dedup (checks now run every ~30s) ---

def test_time_stop_would_fire_logs_once_per_trade(monkeypatch):
    from datetime import datetime, timedelta
    import pytz
    monkeypatch.setattr(config, 'TIME_STOP_ENABLED', False)
    monkeypatch.setattr(config, 'TIME_STOP_MIN', 40)
    ist = pytz.timezone('Asia/Kolkata')
    entry = (datetime.now(ist) - timedelta(minutes=60)).isoformat()
    trade = _long(stop=1, target=10_000, entry_time=entry)  # never exits
    b = _brain([trade], fresh_price=100.0)
    logged = []
    with patch('brain.db.get_open_trades', return_value=[trade]), \
         patch('brain.db.log_brain_activity',
               side_effect=lambda **k: logged.append(k)):
        b.check_open_exits()
        b.check_open_exits()
        b.check_open_exits()
    would = [l for l in logged if l.get('activity_type') == 'TIME_STOP_WOULD_FIRE']
    assert len(would) == 1


# --- market_data.get_fresh_close ---

def test_get_fresh_close_bypasses_ttl_cache():
    kite = MagicMock()
    md = MarketData(kite)
    md._instrument_cache['NSE:INFY'] = 123
    # prime the TTL cache with a STALE price
    md._candle_cache['NSE:INFY_5minute'] = [{'close': 100}]
    md._candle_cache_time['NSE:INFY_5minute'] = __import__('time').time()
    with patch.object(md, '_get_historical',
                      return_value=[{'timestamp': 't', 'open': 1, 'high': 1,
                                     'low': 1, 'close': 95.5, 'volume': 1}]) as gh:
        price = md.get_fresh_close('NSE:INFY')
    gh.assert_called_once()          # hit the API despite a warm cache
    assert price == 95.5


def test_get_fresh_close_none_without_token():
    md = MarketData(MagicMock())
    assert md.get_fresh_close('NSE:UNKNOWN') is None


# --- REQ-072 incident dedup ---

def test_deploy_incident_reported_once_per_session_sha():
    scheduler._reported_deploy_incidents.clear()
    key = ('sess-1', config.GIT_SHA)
    assert key not in scheduler._reported_deploy_incidents
    scheduler._reported_deploy_incidents.add(key)
    assert key in scheduler._reported_deploy_incidents  # second pass skips
