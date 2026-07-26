"""Per-stock agent — a persistent, always-on observation timeline for every
holding (VISION: 24/7 per-stock tracking, mechanical-first).

WHAT THIS IS: the advisor gives a one-shot daily verdict from a cold snapshot.
This module instead accumulates a *timeline* of mechanical observations per
symbol — captured on every advisor pass (hourly-deduped intraday, plus the
daily run) — so the morning verdict can read how the stock has been evolving,
not just where it sits today.

DELIBERATELY MECHANICAL: no LLM, no I/O here. `build_observation` repackages
the indicators the advisor already computes into a timeline row; `summarize_
timeline` is a pure read over recent rows. Storage lives in database.py. The
`fundamentals` slot is null until a provider is wired (roadmap P3); news is
carried through whenever the collector is keyed.
"""
from datetime import datetime, time

import pytz

IST = pytz.timezone('Asia/Kolkata')

# NSE regular session (IST). Before = pre-open, after = post-close; these
# label each capture so the timeline knows what kind of observation it is.
_MARKET_OPEN = time(9, 15)
_MARKET_CLOSE = time(15, 30)

# How much a trend_score has to move across the window before we call it a
# direction rather than noise.
_DIRECTION_EPS = 5


def observation_phase(now_ist: datetime = None) -> str:
    """PRE_OPEN / INTRADAY / POST_CLOSE for the given IST moment."""
    t = (now_ist or datetime.now(IST)).time()
    if t < _MARKET_OPEN:
        return 'PRE_OPEN'
    if t <= _MARKET_CLOSE:
        return 'INTRADAY'
    return 'POST_CLOSE'


def build_observation(symbol: str, advice: dict, sector: str = None,
                      phase: str = None, observed_at: datetime = None) -> dict:
    """One timeline row from an advice dict (the mechanical state the advisor
    already produced). Pure — shaped to the stock_observations columns."""
    ind = advice.get('indicators') or {}
    ts = observed_at or datetime.now(IST)
    return {
        'symbol': symbol,
        'observed_at': ts.isoformat(),
        'phase': phase or observation_phase(ts),
        'price': advice.get('last_price'),
        'trend_score': advice.get('trend_score'),
        'verdict': advice.get('verdict'),
        'payload': {
            'technical': {
                'ema_50': ind.get('ema_50'),
                'ema_200': ind.get('ema_200'),
                'rsi_14': ind.get('rsi_14'),
                'adx': ind.get('adx'),
                'trend_consistency_pct': ind.get('trend_consistency_pct'),
                'relative_strength_vs_nifty': ind.get('relative_strength_vs_nifty'),
                'volume_trend_ratio': ind.get('volume_trend_ratio'),
                'support': ind.get('support'),
                'resistance': ind.get('resistance'),
            },
            'macro': {
                'market_regime': advice.get('market_regime'),
                'sector': sector,
                'weekly_trend': ind.get('weekly_trend'),
                'daily_weekly_alignment': ind.get('daily_weekly_alignment'),
            },
            'news': {'sentiment': ind.get('news_sentiment')},
            'fundamentals': None,   # pluggable — filled when a provider lands (P3)
            'confidence': advice.get('confidence'),
        },
    }


def summarize_timeline(rows: list) -> dict:
    """Pure read over a symbol's recent observations (any order) — the
    evolution context the morning verdict reads: which way trend_score has
    drifted, whether the verdict flipped, and net news tone."""
    obs = sorted(rows or [], key=lambda r: r.get('observed_at') or '')
    n = len(obs)
    if not n:
        return {'observations': 0}

    scores = [r['trend_score'] for r in obs if r.get('trend_score') is not None]
    verdicts = [r['verdict'] for r in obs if r.get('verdict')]
    score_change = (scores[-1] - scores[0]) if len(scores) >= 2 else 0
    direction = ('rising' if score_change > _DIRECTION_EPS
                 else 'falling' if score_change < -_DIRECTION_EPS else 'flat')

    sentiments = []
    for r in obs:
        s = ((r.get('payload') or {}).get('news') or {}).get('sentiment')
        if s is not None:
            sentiments.append(s)

    distinct_verdicts = list(dict.fromkeys(verdicts))   # order-preserving unique
    return {
        'observations': n,
        'span_from': obs[0].get('observed_at'),
        'span_to': obs[-1].get('observed_at'),
        'trend_score_direction': direction,
        'trend_score_change': score_change,
        'latest_trend_score': scores[-1] if scores else None,
        'verdict_changed': len(distinct_verdicts) > 1,
        'verdict_path': distinct_verdicts[-4:],
        'avg_news_sentiment': (round(sum(sentiments) / len(sentiments), 3)
                               if sentiments else None),
    }
