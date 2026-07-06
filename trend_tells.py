"""Mechanical trend-day tells (ENGINEERING_SPEC REQ-052).

Four independent, mechanical checks that a genuine trend day is underway.
On a real trend day these agree and override the softer regime call for
entry permission (spec: 3 of 4 required). Every function is pure and
individually testable; each returns True / False / None, where **None means
"data to decide this tell is unavailable"** (abstain), NOT False.

Status during the current paper run: computed and LOGGED on every decision
(non-gating) so the tells can be validated against outcomes in the M5
backtest. Entry-gating is switched on later, once the data confirms they
carry signal — per the "build + test now, adopt when learning" plan.

Candle = dict with open/high/low/close/volume (market_data.get_candles).
"""

import config


def vwap(candles: list):
    """Volume-weighted average price over the given candles. None if no
    volume (can't weight)."""
    num = 0.0
    den = 0.0
    for c in candles:
        typical = (c['high'] + c['low'] + c['close']) / 3.0
        vol = c.get('volume') or 0
        num += typical * vol
        den += vol
    return (num / den) if den > 0 else None


def tell_vwap_persistence(candles: list, direction: str,
                          min_fraction: float = None):
    """Price persistently on the trend side of VWAP. UP: most candle closes
    above a running VWAP; DOWN: below. Returns None if too few candles or
    no volume."""
    min_fraction = (config.VWAP_PERSISTENCE_FRAC
                    if min_fraction is None else min_fraction)
    if not candles or len(candles) < 5 or direction not in ('UP', 'DOWN'):
        return None
    running_num = 0.0
    running_den = 0.0
    on_side = 0
    counted = 0
    for c in candles:
        typical = (c['high'] + c['low'] + c['close']) / 3.0
        vol = c.get('volume') or 0
        running_num += typical * vol
        running_den += vol
        if running_den <= 0:
            continue
        vw = running_num / running_den
        counted += 1
        if direction == 'UP' and c['close'] >= vw:
            on_side += 1
        elif direction == 'DOWN' and c['close'] <= vw:
            on_side += 1
    if counted < 5:
        return None
    return (on_side / counted) >= min_fraction


def tell_gap_hold(prev_close, today_open, current_price, direction: str):
    """A gap in the trend direction that has HELD — price still beyond the
    open on the gap side. Returns None if inputs missing."""
    if not prev_close or not today_open or not current_price:
        return None
    if direction == 'UP':
        gapped = today_open > prev_close
        held = current_price >= today_open
        return bool(gapped and held)
    if direction == 'DOWN':
        gapped = today_open < prev_close
        held = current_price <= today_open
        return bool(gapped and held)
    return None


def tell_range_expansion(today_range, avg_range,
                         threshold: float = None):
    """Today's range is expanding vs the recent average (trend days run
    wider than chop). Returns None if the average is unknown."""
    threshold = (config.RANGE_EXPANSION_THRESHOLD
                 if threshold is None else threshold)
    if not avg_range or avg_range <= 0 or today_range is None:
        return None
    return (today_range / avg_range) >= threshold


def tell_breadth_sector(advancers, decliners, sector_aligned, direction: str):
    """Market breadth + the symbol's sector agree with the trade direction.
    Needs index-breadth + sector data — UNAVAILABLE with retail enctoken, so
    this abstains (None) in the current setup. Wired for when Kite Connect
    (M0) unlocks the data feed."""
    if advancers is None or decliners is None or sector_aligned is None:
        return None
    if direction == 'UP':
        breadth_ok = advancers > decliners
    elif direction == 'DOWN':
        breadth_ok = decliners > advancers
    else:
        return None
    return bool(breadth_ok and sector_aligned)


def session_range(candles: list):
    """High-low range across the candles (a session's realized range)."""
    if not candles:
        return None
    return max(c['high'] for c in candles) - min(c['low'] for c in candles)


def evaluate(direction: str, candles_5min: list = None,
             prev_close=None, today_open=None, current_price=None,
             today_range=None, avg_range=None,
             advancers=None, decliners=None, sector_aligned=None) -> dict:
    """Run all four tells for a direction. Returns:
      tells: {name: True|False|None}
      fired: count of True
      available: count of non-None (tells that could be decided)
      required: config.TREND_TELLS_REQUIRED
      trend_day: fired >= required (only meaningful when available >= required)
      permits_entry: trend_day AND direction in (UP, DOWN)
    """
    tells = {
        'vwap_persistence': tell_vwap_persistence(candles_5min or [], direction),
        'gap_hold': tell_gap_hold(prev_close, today_open, current_price, direction),
        'range_expansion': tell_range_expansion(today_range, avg_range),
        'breadth_sector': tell_breadth_sector(
            advancers, decliners, sector_aligned, direction),
    }
    fired = sum(1 for v in tells.values() if v is True)
    available = sum(1 for v in tells.values() if v is not None)
    required = config.TREND_TELLS_REQUIRED
    trend_day = fired >= required
    return {
        'tells': tells,
        'fired': fired,
        'available': available,
        'required': required,
        'direction': direction,
        'trend_day': trend_day,
        'permits_entry': bool(trend_day and direction in ('UP', 'DOWN')),
    }
