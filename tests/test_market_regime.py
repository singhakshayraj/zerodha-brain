"""Market Regime Filter — classification, adaptations, and the fail-safe
NEUTRAL guarantee. ADVISORY ONLY — nothing here touches an order path."""
import os
from unittest.mock import MagicMock, patch

with patch.dict(os.environ, {
    'SUPABASE_URL': 'https://fake.supabase.co',
    'SUPABASE_SERVICE_KEY': 'fake-key',
}):
    with patch('supabase.create_client', return_value=MagicMock()):
        import database  # noqa

import config
import market_regime as mr
import portfolio_advisor as pa


def _candles(closes, spread_pct=0.5):
    """Daily candles with a controlled high-low range (drives ATR)."""
    out = []
    for i, c in enumerate(closes):
        r = c * spread_pct / 100
        out.append({'timestamp': f'2026-01-{i % 28 + 1:02d}',
                    'open': c, 'high': c + r, 'low': c - r,
                    'close': c, 'volume': 1000})
    return out


def _trending_up(n=60, start=100.0, step=1.0):
    return [start + i * step for i in range(n)]


def test_aggressive_bull_detected():
    # Steady climb: price > EMA20, directional movement strong -> high ADX.
    out = mr.get_market_regime(_candles(_trending_up()))
    assert out['regime'] == 'AGGRESSIVE_BULL'
    assert out['adx'] >= config.REGIME_ADX_TREND
    assert out['ema20_dist_pct'] > 0


def test_aggressive_bear_detected():
    closes = [160.0 - i * 1.0 for i in range(60)]
    out = mr.get_market_regime(_candles(closes))
    assert out['regime'] == 'AGGRESSIVE_BEAR'
    assert out['ema20_dist_pct'] < 0


def test_choppy_sideways_detected():
    # Tiny alternating oscillation: no direction (low ADX), narrow ATR.
    closes = [100.0 + (0.3 if i % 2 else -0.3) for i in range(60)]
    out = mr.get_market_regime(_candles(closes, spread_pct=0.4))
    assert out['regime'] == 'CHOPPY_SIDEWAYS'
    assert out['adx'] < config.REGIME_ADX_CHOP
    assert out['atr_pct'] <= config.REGIME_ATR_QUIET_PCT


def test_panic_detected_and_overrides_trend():
    # Violent swings with wide daily ranges: ATR% >= panic threshold wins
    # even though direction terms might read trendy.
    closes = [100.0 + (4.0 if i % 2 else -4.0) for i in range(60)]
    out = mr.get_market_regime(_candles(closes, spread_pct=5.0))
    assert out['regime'] == 'HIGH_VOLATILITY_PANIC'
    assert out['atr_pct'] >= config.REGIME_ATR_PANIC_PCT


def test_fail_safe_neutral():
    assert mr.get_market_regime([])['regime'] == 'NEUTRAL'
    assert mr.get_market_regime(None)['regime'] == 'NEUTRAL'
    assert mr.get_market_regime([{'close': None}] * 40)['regime'] == 'NEUTRAL'
    # too little history
    assert mr.get_market_regime(_candles([100.0] * 10))['regime'] == 'NEUTRAL'


def test_disabled_flag_yields_neutral():
    with patch.object(config, 'REGIME_FILTER_ENABLED', False):
        out = mr.get_market_regime(_candles(_trending_up()))
    assert out['regime'] == 'NEUTRAL'


def test_rotation_gap_widens_only_in_chop():
    assert mr.rotation_min_gap_for('CHOPPY_SIDEWAYS') == config.ROTATION_MIN_GAP_CHOPPY
    for r in ('NEUTRAL', 'AGGRESSIVE_BULL', 'AGGRESSIVE_BEAR',
              'HIGH_VOLATILITY_PANIC', None):
        assert mr.rotation_min_gap_for(r) == config.ROTATION_MIN_GAP


def test_score_weights_identity_outside_panic():
    for r in ('NEUTRAL', 'AGGRESSIVE_BULL', 'CHOPPY_SIDEWAYS', None):
        assert mr.score_weights_for(r) == {'ema200': 1.0, 'momentum': 1.0}
    w = mr.score_weights_for('HIGH_VOLATILITY_PANIC')
    assert w['ema200'] > 1.0 and w['momentum'] < 1.0


# ── trend_score regime integration ──────────────────────────────────────────

_IND_UP = {'current_close': 120.0, 'ema_200': 100.0, 'ema_50': 110.0,
           'adx': 30, 'adx_plus_di': 30, 'adx_minus_di': 10}


def _closes_up():
    return [100.0 + i for i in range(30)]


def test_trend_score_unchanged_without_regime():
    """REGRESSION PIN: regime=None must be bit-for-bit the pre-upgrade score."""
    base = pa.trend_score(_IND_UP, _closes_up(), consistency=80,
                          rel_strength=4.0)
    with_none = pa.trend_score(_IND_UP, _closes_up(), consistency=80,
                               rel_strength=4.0, regime=None)
    with_neutral = pa.trend_score(_IND_UP, _closes_up(), consistency=80,
                                  rel_strength=4.0, regime='NEUTRAL')
    assert base == with_none == with_neutral


def test_trend_score_panic_favors_ema200_damps_momentum():
    # Price above EMA200 but with strong 20-bar momentum: in panic the
    # EMA200 term grows (20->30) and the momentum term shrinks (cap 20->10).
    closes = _closes_up()
    normal = pa.trend_score(_IND_UP, closes)
    panic = pa.trend_score(_IND_UP, closes, regime='HIGH_VOLATILITY_PANIC')
    # momentum here saturates its cap (fast climb): term goes 20 -> 10 (−10);
    # ema200 term goes 20 -> 30 (+10) — net zero for an aligned name...
    assert isinstance(panic, int)
    # ...but a name BELOW its EMA200 riding a hot short-term bounce must
    # score materially worse in panic (the whole point of the reweight):
    ind_below = {**_IND_UP, 'ema_200': 130.0}
    normal_below = pa.trend_score(ind_below, closes)
    panic_below = pa.trend_score(ind_below, closes,
                                 regime='HIGH_VOLATILITY_PANIC')
    assert panic_below < normal_below
    assert (normal - panic) in (0, 20)  # bounded, structured shift only


def test_advise_stamps_regime_and_trigger():
    candles = _candles(_trending_up(80))
    out = pa.advise({'symbol': 'X', 'quantity': 10, 'average_price': 100,
                     'last_price': 179}, candles, regime='AGGRESSIVE_BULL')
    assert out['market_regime'] == 'AGGRESSIVE_BULL'
    assert out['trigger_type'] in ('MACRO', 'MICRO')
    # insufficient path carries the columns too (uniform upsert keys)
    short = pa.advise({'symbol': 'X', 'quantity': 10, 'average_price': 100,
                       'last_price': 100}, candles[:5], regime='NEUTRAL')
    assert short['market_regime'] == 'NEUTRAL'
    assert short['trigger_type'] is None
