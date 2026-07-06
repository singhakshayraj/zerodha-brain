"""Level pack — ENGINEERING_SPEC §2/M3, nightly per liquid-universe stock.

Pure computation over daily/intraday candles. Produces the reference levels
the strategy pipeline anchors stops and targets to (M4 step 6/7): prior-day
high/low/close, round-number levels, ATR, weekly high/low, gap zone.

Runs on the Mac (scripts/build_level_pack.py) → Supabase. No live state, no
network here — just the math, so it is fully unit-testable.

Candle = dict with open/high/low/close/volume/timestamp.
"""

from indicators import calculate_atr


def _by_date(candles: list) -> dict:
    """Group candles by trade-date (timestamp 'YYYY-MM-DD...'), ordered."""
    out = {}
    for c in candles or []:
        day = (c.get('timestamp') or '')[:10]
        if day:
            out.setdefault(day, []).append(c)
    return out


def daily_ohlc(candles: list) -> list:
    """Collapse intraday candles to one OHLC per date, date-ascending."""
    days = _by_date(candles)
    out = []
    for day in sorted(days):
        cs = days[day]
        out.append({
            'date': day,
            'open': cs[0]['open'],
            'high': max(c['high'] for c in cs),
            'low': min(c['low'] for c in cs),
            'close': cs[-1]['close'],
            'volume': sum(c.get('volume') or 0 for c in cs),
        })
    return out


def prior_day_levels(daily: list) -> dict:
    """PDH/PDL/PDC from the most recent COMPLETED day (the last in a
    date-ascending daily series)."""
    if not daily:
        return {'pdh': None, 'pdl': None, 'pdc': None}
    prev = daily[-1]
    return {'pdh': prev['high'], 'pdl': prev['low'], 'pdc': prev['close']}


def round_levels(price: float, count: int = 3) -> list:
    """Nearest psychological round levels around a price. Step scales with
    magnitude (₹10 stocks cluster at 1s, ₹3000 stocks at 50s)."""
    if not price or price <= 0:
        return []
    if price < 50:
        step = 1
    elif price < 200:
        step = 5
    elif price < 1000:
        step = 10
    elif price < 3000:
        step = 50
    else:
        step = 100
    base = round(price / step) * step
    out = []
    for k in range(-count, count + 1):
        lvl = base + k * step
        if lvl > 0:
            out.append(round(lvl, 2))
    return sorted(set(out))


def weekly_high_low(daily: list, sessions: int = 5) -> dict:
    window = daily[-sessions:] if daily else []
    if not window:
        return {'weekly_high': None, 'weekly_low': None}
    return {
        'weekly_high': max(d['high'] for d in window),
        'weekly_low': min(d['low'] for d in window),
    }


def gap_levels(daily: list) -> dict:
    """Gap zone between the prior close and the latest open (if the series
    has an unfinished/latest day). Uses the last two daily rows."""
    if not daily or len(daily) < 2:
        return {}
    prev_close = daily[-2]['close']
    last_open = daily[-1]['open']
    if not prev_close or not last_open:
        return {}
    gap_pct = (last_open - prev_close) / prev_close * 100
    return {
        'gap_from': round(min(prev_close, last_open), 2),
        'gap_to': round(max(prev_close, last_open), 2),
        'gap_pct': round(gap_pct, 3),
        'direction': 'UP' if last_open > prev_close else 'DOWN' if last_open < prev_close else 'FLAT',
    }


def vol_curve(daily: list, days: int = 20) -> dict:
    window = daily[-days:] if daily else []
    vols = [d.get('volume') or 0 for d in window]
    if not vols:
        return {}
    avg = sum(vols) / len(vols)
    return {'avg_volume': round(avg, 2), 'samples': len(vols),
            'last_volume': vols[-1]}


def build(symbol: str, date: str, daily_candles: list) -> dict:
    """Assemble a level_pack row from a date-ascending DAILY candle series
    (the prior N sessions, latest last). Returns a dict ready to upsert."""
    daily = daily_candles or []
    pdl = prior_day_levels(daily)
    wk = weekly_high_low(daily)
    atr = calculate_atr(daily, period=14) if len(daily) >= 15 else None
    ref_price = pdl['pdc'] or (daily[-1]['close'] if daily else 0)
    return {
        'symbol': symbol,
        'date': date,
        'pdh': pdl['pdh'], 'pdl': pdl['pdl'], 'pdc': pdl['pdc'],
        'gap_levels': gap_levels(daily),
        'round_levels': round_levels(ref_price),
        'atr14': atr,
        'vol_curve_20d': vol_curve(daily),
        'weekly_high': wk['weekly_high'],
        'weekly_low': wk['weekly_low'],
    }
