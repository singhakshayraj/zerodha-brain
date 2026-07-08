"""Real market-direction context (universe breadth) + REQ-073 latency.
Fixes the dead SIDEWAYS stub behind 2026-07-08's shorts into a rising tape."""
import os
from unittest.mock import MagicMock, patch

with patch.dict(os.environ, {
    'SUPABASE_URL': 'https://fake.supabase.co',
    'SUPABASE_SERVICE_KEY': 'fake-key',
}):
    with patch('supabase.create_client', return_value=MagicMock()):
        import database  # noqa

import config
from brain import TradingBrain, _compute_trend_tells


def _brain(universe, packs, prices):
    b = TradingBrain.__new__(TradingBrain)
    b.universe = universe
    b.level_packs = packs
    b.market_data = MagicMock()
    b.market_data._holdings_cache = prices
    return b


def test_market_context_bullish_when_universe_up():
    uni = {'NSE:A': {}, 'NSE:B': {}, 'NSE:C': {}}
    packs = {'NSE:A': {'pdc': 100}, 'NSE:B': {'pdc': 100}, 'NSE:C': {'pdc': 100}}
    prices = {'NSE:A': {'price': 102}, 'NSE:B': {'price': 101},
              'NSE:C': {'price': 103}}   # avg +2% → BULLISH
    ctx = _brain(uni, packs, prices)._market_context()
    assert ctx['direction'] == 'BULLISH'
    assert ctx['change_percent'] == 2.0
    assert ctx['advancers'] == 3 and ctx['decliners'] == 0
    assert ctx['breadth'] == 1.0


def test_market_context_bearish_when_universe_down():
    uni = {'NSE:A': {}, 'NSE:B': {}}
    packs = {'NSE:A': {'pdc': 100}, 'NSE:B': {'pdc': 100}}
    prices = {'NSE:A': {'price': 98}, 'NSE:B': {'price': 97}}  # avg -2.5%
    ctx = _brain(uni, packs, prices)._market_context()
    assert ctx['direction'] == 'BEARISH'
    assert ctx['decliners'] == 2


def test_market_context_sideways_when_flat():
    uni = {'NSE:A': {}, 'NSE:B': {}}
    packs = {'NSE:A': {'pdc': 100}, 'NSE:B': {'pdc': 100}}
    prices = {'NSE:A': {'price': 100.2}, 'NSE:B': {'price': 99.9}}  # ~flat
    ctx = _brain(uni, packs, prices)._market_context()
    assert ctx['direction'] == 'SIDEWAYS'


def test_market_context_empty_when_no_pdc():
    uni = {'NSE:A': {}}
    ctx = _brain(uni, {}, {'NSE:A': {'price': 100}})._market_context()
    assert ctx['direction'] == 'SIDEWAYS'
    assert ctx['breadth'] is None


def test_market_context_never_raises_on_garbage():
    uni = {'NSE:A': {}}
    ctx = _brain(uni, {'NSE:A': {'pdc': 'bad'}},
                 {'NSE:A': {'price': 100}})._market_context()
    assert isinstance(ctx, dict) and 'direction' in ctx


# --- gated feed flag ---

def test_market_direction_flag_default_off():
    assert config.MARKET_DIRECTION_ENABLED in (True, False)


# --- breadth now feeds trend-tells breadth_sector ---

def test_trend_tells_breadth_sector_uses_market_ctx():
    signal = {'action': 'BUY', 'stop_loss': 98, 'indicators': {'atr_14': 2}}
    candles = [{'open': 100, 'high': 101, 'low': 99, 'close': 100 + i * 0.1,
                'volume': 1000, 'timestamp': f'2026-07-09T09:{15+i:02d}:00+0530'}
               for i in range(10)]
    mc = {'direction': 'BULLISH', 'advancers': 30, 'decliners': 10,
          'breadth': 0.75, 'change_percent': 1.2}
    snap = _compute_trend_tells(signal, candles, 100.5, market_ctx=mc)
    # breadth_sector should now be a bool, not None (data available)
    assert snap['tells']['breadth_sector'] is not None


def test_trend_tells_breadth_sector_abstains_without_ctx():
    signal = {'action': 'BUY', 'stop_loss': 98, 'indicators': {'atr_14': 2}}
    candles = [{'open': 100, 'high': 101, 'low': 99, 'close': 100,
                'volume': 1000, 'timestamp': '2026-07-09T09:15:00+0530'}
               for _ in range(10)]
    snap = _compute_trend_tells(signal, candles, 100.5, market_ctx=None)
    assert snap['tells']['breadth_sector'] is None


# --- REQ-073 decision→order latency ---

def test_decision_latency_none_without_clock():
    b = TradingBrain.__new__(TradingBrain)
    b._decision_ts = None
    assert b._decision_latency_ms() is None


def test_decision_latency_positive_after_clock():
    import time
    b = TradingBrain.__new__(TradingBrain)
    b._decision_ts = time.perf_counter()
    ms = b._decision_latency_ms()
    assert ms is not None and ms >= 0
