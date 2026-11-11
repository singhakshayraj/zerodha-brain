"""Sector-aware verdict (trial 261) — v1's call nudged by its sector's trend.

Pre-registered 2026-11-11 before any grade; thresholds are fixed here and
must not be tuned on outcomes. Sector strength = median advisor trend score of
the sector group's names on the same day (sector_view.group_of groups).
Groups with fewer than MIN_NAMES names are never tilted.

  SELL / SELL_ON_BOUNCE, sector >= STRONG               -> TRIM
  TRIM, sector >= STRONG and stock trend > 0             -> HOLD
  TRIM, sector <= WEAK   and stock trend < 0             -> SELL
  HOLD, sector <= WEAK   and stock trend < 0             -> TRIM
  anything else                                          -> unchanged
"""
from statistics import median

import sector_view

STRONG, WEAK, MIN_NAMES = 30, -30, 5


def tilt(verdict: str, stock_trend, sector_trend):
    """-> (verdict, rule or None). Pure."""
    if sector_trend is None or verdict not in ('SELL', 'SELL_ON_BOUNCE', 'TRIM', 'HOLD'):
        return verdict, None
    t = stock_trend or 0
    if verdict in ('SELL', 'SELL_ON_BOUNCE') and sector_trend >= STRONG:
        return 'TRIM', 'strong_sector_softens_sell'
    if verdict == 'TRIM' and sector_trend >= STRONG and t > 0:
        return 'HOLD', 'strong_sector_keeps_trim'
    if verdict == 'TRIM' and sector_trend <= WEAK and t < 0:
        return 'SELL', 'weak_sector_hardens_trim'
    if verdict == 'HOLD' and sector_trend <= WEAK and t < 0:
        return 'TRIM', 'weak_sector_trims_hold'
    return verdict, None


def sector_trends(items: list, sector_of: dict) -> dict:
    """{group: median trend} from [{symbol, trend_score}], groups with at
    least MIN_NAMES scored names only."""
    by = {}
    for it in items or []:
        if it.get('trend_score') is None:
            continue
        g = sector_view.group_of(it['symbol'], sector_of.get(it['symbol']))
        by.setdefault(g, []).append(it['trend_score'])
    return {g: round(median(v)) for g, v in by.items() if len(v) >= MIN_NAMES}


def apply(rows: list, trends: dict, sector_of: dict) -> int:
    """Set sector_verdict / sector_trend / sector_rule on each row (keeps
    `verdict` untouched). Returns rows whose verdict changed."""
    changed = 0
    for r in rows:
        g = sector_view.group_of(r['symbol'], sector_of.get(r['symbol']))
        st = trends.get(g)
        v, rule = tilt(r.get('verdict'), r.get('trend_score'), st)
        r['sector_verdict'], r['sector_trend'], r['sector_rule'] = v, st, rule
        changed += v != r.get('verdict')
    return changed
