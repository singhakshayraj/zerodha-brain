"""In-play list — ENGINEERING_SPEC §2.1/M3, locked once at 09:30 IST.

Ranks the universe by opening-range relative volume (RVOL) and keeps the top
N as the day's tradeable in-play set. Pure ranking here; the 09:30 locker
(brain/scheduler) fetches opening-range data and calls rank(), then writes
inplay_list. Kept separate + pure so ranking is testable without market data.
"""

import config

# Opening range = first OR_MINUTES of the session (spec §2.1: 09:15–09:30).
OR_MINUTES = 15


def _by_date(candles: list) -> dict:
    out = {}
    for c in candles or []:
        day = (c.get('timestamp') or '')[:10]
        if day:
            out.setdefault(day, []).append(c)
    return out


def _or_slice(day_candles: list) -> list:
    """First OR_MINUTES worth of 5-minute candles (3 for a 15m OR)."""
    return day_candles[:max(1, OR_MINUTES // 5)]


def opening_range_stats(candles_5min: list) -> dict:
    """Today's opening-range stats + RVOL baseline from the prior days
    present in the same candle window. Pure — feed it get_candles output.

    Returns {} when today has no candles. avg_or_volume needs >= 2 prior
    days, else or_rvol is None (unranked, excluded from in-play)."""
    days = _by_date(candles_5min)
    if not days:
        return {}
    ordered = sorted(days)
    today = ordered[-1]
    today_or = _or_slice(days[today])

    or_volume = sum(c.get('volume') or 0 for c in today_or)
    or_high = max(c['high'] for c in today_or)
    or_low = min(c['low'] for c in today_or)

    prior_or_vols = []
    for d in ordered[:-1]:
        vols = sum(c.get('volume') or 0 for c in _or_slice(days[d]))
        if vols > 0:
            prior_or_vols.append(vols)
    avg_or_volume = (sum(prior_or_vols) / len(prior_or_vols)
                     if len(prior_or_vols) >= 2 else None)

    prev_day = ordered[-2] if len(ordered) >= 2 else None
    prev_close = days[prev_day][-1]['close'] if prev_day else None
    today_open = days[today][0]['open']
    gap_pct = (round((today_open - prev_close) / prev_close * 100, 3)
               if prev_close else None)

    return {
        'or_volume': or_volume,
        'avg_or_volume': avg_or_volume,
        'or_rvol': opening_range_rvol(or_volume, avg_or_volume),
        'or_high': or_high,
        'or_low': or_low,
        'gap_pct': gap_pct,
    }


def opening_range_rvol(or_volume: float, avg_or_volume: float):
    """RVOL of the opening range: today's OR volume vs the recent average OR
    volume for this symbol. None if no baseline."""
    if not avg_or_volume or avg_or_volume <= 0:
        return None
    return round(or_volume / avg_or_volume, 4)


def rank(candidates: list, cap: int = None, min_rvol: float = None) -> list:
    """candidates: list of dicts with symbol, or_rvol (float|None),
    gap_pct, or_high, or_low. Returns the top `cap` by or_rvol, filtered to
    or_rvol >= min_rvol, each stamped with 1-based `rank`.

    Symbols with unknown RVOL (None) are excluded — an unranked symbol is not
    an in-play symbol."""
    cap = cap if cap is not None else config.INPLAY_CAP
    min_rvol = min_rvol if min_rvol is not None else config.RVOL_THRESHOLD

    ranked = sorted(
        (c for c in candidates
         if c.get('or_rvol') is not None and c['or_rvol'] >= min_rvol),
        key=lambda c: c['or_rvol'],
        reverse=True,
    )[:cap]

    out = []
    for i, c in enumerate(ranked, start=1):
        row = dict(c)
        row['rank'] = i
        out.append(row)
    return out
