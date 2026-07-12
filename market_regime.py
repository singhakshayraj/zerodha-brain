"""Market Regime Filter — classify the Nifty 50 tape before advising.

The advisor's 7-factor score treats every day the same, but the same signal
means different things in different tapes: a 40pt rotation gap is conviction
in a trending market and noise in a sideways one; short-term momentum is
information in a trend and a trap in a panic. This module reads the index
once per run and hands the advisor a regime label plus the adaptations.

Regimes (on Nifty 50 daily bars):
  AGGRESSIVE_BULL        price > 20-EMA and ADX >= REGIME_ADX_TREND
  AGGRESSIVE_BEAR        price < 20-EMA and ADX >= REGIME_ADX_TREND
  HIGH_VOLATILITY_PANIC  ATR% of price >= REGIME_ATR_PANIC_PCT (checked first
                         — a wide-range tape overrides everything else)
  CHOPPY_SIDEWAYS        ADX < REGIME_ADX_CHOP and ATR% <= REGIME_ATR_QUIET_PCT
  NEUTRAL                anything else, and the fail-safe on any error

NEUTRAL changes nothing — every adaptation below is identity under NEUTRAL,
so a regime-detection failure can never alter a verdict. ADVISORY ONLY.
"""
import config
from indicators import calculate_adx, calculate_atr, calculate_ema, get_closes

REGIMES = ('AGGRESSIVE_BULL', 'AGGRESSIVE_BEAR', 'CHOPPY_SIDEWAYS',
           'HIGH_VOLATILITY_PANIC', 'NEUTRAL')

# HIGH_VOLATILITY_PANIC score reweighting: long-term structure gets louder,
# short-term momentum gets quieter. Identity (1.0) in every other regime.
PANIC_EMA200_WEIGHT = 1.5     # ±20 -> ±30: the 200-day side is the anchor
PANIC_MOMENTUM_WEIGHT = 0.5   # ±20 -> ±10: 20-bar momentum is whipsaw fodder


def get_market_regime(nifty_candles: list) -> dict:
    """Classify the index tape from its daily candles. Pure; never raises —
    any failure (thin history, bad data) returns NEUTRAL with null inputs."""
    out = {'regime': 'NEUTRAL', 'adx': None, 'atr_pct': None,
           'ema20_dist_pct': None}
    try:
        if not config.REGIME_FILTER_ENABLED:
            return out
        closes = get_closes(nifty_candles or [])
        if len(closes) < 30:
            return out
        price = closes[-1]
        adx_block = calculate_adx(nifty_candles, 14) or {}
        adx = adx_block.get('adx')
        atr = calculate_atr(nifty_candles, 14)
        ema20 = calculate_ema(closes, 20)
        if not price or adx is None or atr is None or not ema20:
            return out

        atr_pct = round(atr / price * 100, 2)
        ema20_dist_pct = round((price - ema20) / ema20 * 100, 2)
        out.update(adx=adx, atr_pct=atr_pct, ema20_dist_pct=ema20_dist_pct)

        if atr_pct >= config.REGIME_ATR_PANIC_PCT:
            out['regime'] = 'HIGH_VOLATILITY_PANIC'
        elif adx >= config.REGIME_ADX_TREND:
            out['regime'] = ('AGGRESSIVE_BULL' if price > ema20
                             else 'AGGRESSIVE_BEAR')
        elif adx < config.REGIME_ADX_CHOP and atr_pct <= config.REGIME_ATR_QUIET_PCT:
            out['regime'] = 'CHOPPY_SIDEWAYS'
        return out
    except Exception as e:
        print(f"[regime] detection failed (fail-safe NEUTRAL): {e}")
        return {'regime': 'NEUTRAL', 'adx': None, 'atr_pct': None,
                'ema20_dist_pct': None}


def rotation_min_gap_for(regime: str) -> int:
    """The rotation gate's gap requirement under this regime. Wider in chop:
    trendless tapes generate large transient score spreads that mean-revert —
    demanding 65pts instead of 40 filters churn, not opportunity."""
    if regime == 'CHOPPY_SIDEWAYS':
        return config.ROTATION_MIN_GAP_CHOPPY
    return config.ROTATION_MIN_GAP


def score_weights_for(regime: str) -> dict:
    """Per-factor weight multipliers for trend_score under this regime.
    Identity everywhere except HIGH_VOLATILITY_PANIC, where the 200-day
    anchor is amplified and 20-bar momentum damped — in a panic tape the
    short-term slope is the least trustworthy number on the chart."""
    if regime == 'HIGH_VOLATILITY_PANIC':
        return {'ema200': PANIC_EMA200_WEIGHT,
                'momentum': PANIC_MOMENTUM_WEIGHT}
    return {'ema200': 1.0, 'momentum': 1.0}
