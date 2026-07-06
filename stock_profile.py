"""Stock profile — ENGINEERING_SPEC §2/M3, weekly per stock.

Behavioural fingerprint used to bias which archetype/instrument to trade:
trendiness (does it run or chop?), gap-follow rate, and a range profile.
Pure math over a daily series; the ≥30-sample rule (spec M3) falls back to a
supplied universe average when a symbol lacks history.

Runs on the Mac (scripts/build_profiles.py) → Supabase.
"""

MIN_SAMPLES = 30


def efficiency_ratio(closes: list) -> float:
    """Kaufman efficiency ratio: |net move| / sum(|bar moves|). ~1.0 = clean
    trend, ~0 = chop. This is 'trendiness'."""
    if not closes or len(closes) < 2:
        return None
    net = abs(closes[-1] - closes[0])
    path = sum(abs(closes[i] - closes[i - 1]) for i in range(1, len(closes)))
    return round(net / path, 4) if path > 0 else 0.0


def gap_follow_rate(daily: list) -> dict:
    """Of days that gapped (open != prior close), the fraction that closed
    further in the gap direction than they opened."""
    if not daily or len(daily) < 2:
        return {'rate': None, 'samples': 0}
    gaps = 0
    followed = 0
    for i in range(1, len(daily)):
        prev_close = daily[i - 1]['close']
        o = daily[i]['open']
        c = daily[i]['close']
        if not prev_close or o == prev_close:
            continue
        gaps += 1
        if o > prev_close and c > o:
            followed += 1
        elif o < prev_close and c < o:
            followed += 1
    return {'rate': round(followed / gaps, 4) if gaps else None,
            'samples': gaps}


def range_profile(daily: list) -> dict:
    """Distribution of daily ranges as % of close (percentiles for sizing
    stop buffers later)."""
    ranges = []
    for d in daily or []:
        close = d.get('close') or 0
        if close > 0:
            ranges.append((d['high'] - d['low']) / close * 100)
    if not ranges:
        return {'samples': 0}
    ranges.sort()
    n = len(ranges)

    def pct(p):
        return round(ranges[min(n - 1, int(p * n))], 4)

    return {'samples': n, 'p25': pct(0.25), 'p50': pct(0.50),
            'p75': pct(0.75), 'mean': round(sum(ranges) / n, 4)}


def build(symbol: str, asof_date: str, daily_candles: list,
          lookback_days: int, universe_avg: dict = None) -> dict:
    """Assemble a stock_profile row. When samples < MIN_SAMPLES, fall back to
    the supplied universe average for the thin fields (spec M3 ≥30 rule)."""
    daily = daily_candles or []
    closes = [d['close'] for d in daily]
    n = len(daily)

    trendiness = efficiency_ratio(closes)
    gap = gap_follow_rate(daily)
    rp = range_profile(daily)

    fell_back = False
    if n < MIN_SAMPLES and universe_avg:
        # not enough of its own history — borrow the universe average so the
        # pipeline still has a usable prior instead of a null
        if trendiness is None:
            trendiness = universe_avg.get('trendiness')
        if gap['rate'] is None:
            gap = {'rate': universe_avg.get('gap_follow_rate'),
                   'samples': gap['samples']}
        fell_back = True

    return {
        'symbol': symbol,
        'asof_date': asof_date,
        'trendiness': trendiness,
        'gap_follow_rate': gap['rate'],
        'range_profile': rp,
        'sample_sizes': {'days': n, 'gap_days': gap['samples'],
                         'fell_back_to_universe_avg': fell_back},
        'lookback_days': lookback_days,
    }
