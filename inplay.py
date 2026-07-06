"""In-play list — ENGINEERING_SPEC §2.1/M3, locked once at 09:30 IST.

Ranks the universe by opening-range relative volume (RVOL) and keeps the top
N as the day's tradeable in-play set. Pure ranking here; the 09:30 locker
(brain/scheduler) fetches opening-range data and calls rank(), then writes
inplay_list. Kept separate + pure so ranking is testable without market data.
"""

import config


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
