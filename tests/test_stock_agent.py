"""Per-stock agent timeline — pure observation + summary logic."""
from datetime import datetime

import pytz

import stock_agent as sa

IST = pytz.timezone('Asia/Kolkata')


def _at(h, m):
    return IST.localize(datetime(2026, 7, 27, h, m))


def test_observation_phase_boundaries():
    assert sa.observation_phase(_at(8, 0)) == 'PRE_OPEN'
    assert sa.observation_phase(_at(9, 15)) == 'INTRADAY'   # open bell inclusive
    assert sa.observation_phase(_at(12, 30)) == 'INTRADAY'
    assert sa.observation_phase(_at(15, 30)) == 'INTRADAY'   # close inclusive
    assert sa.observation_phase(_at(16, 0)) == 'POST_CLOSE'


def _advice(score, verdict='HOLD', news=None):
    return {
        'last_price': 100.0, 'trend_score': score, 'verdict': verdict,
        'confidence': 70, 'market_regime': 'CHOPPY_SIDEWAYS',
        'indicators': {'ema_50': 98, 'ema_200': 95, 'rsi_14': 55, 'adx': 22,
                       'trend_consistency_pct': 60, 'relative_strength_vs_nifty': 3,
                       'volume_trend_ratio': 1.1, 'support': 96, 'resistance': 104,
                       'weekly_trend': 'UP', 'daily_weekly_alignment': 'ALIGNED_UP',
                       'news_sentiment': news},
    }


def test_build_observation_shape_and_fundamentals_slot():
    obs = sa.build_observation('INFY', _advice(42, 'HOLD', news=0.4),
                               sector='IT', observed_at=_at(10, 0))
    assert obs['symbol'] == 'INFY' and obs['phase'] == 'INTRADAY'
    assert obs['trend_score'] == 42 and obs['verdict'] == 'HOLD' and obs['price'] == 100.0
    p = obs['payload']
    assert p['technical']['rsi_14'] == 55 and p['technical']['adx'] == 22
    assert p['macro']['sector'] == 'IT' and p['macro']['market_regime'] == 'CHOPPY_SIDEWAYS'
    assert p['news']['sentiment'] == 0.4
    assert p['fundamentals'] is None          # pluggable slot, empty until P3
    assert p['confidence'] == 70


def _row(score, verdict, when, news=None):
    return {'observed_at': _at(*when).isoformat(), 'trend_score': score,
            'verdict': verdict, 'payload': {'news': {'sentiment': news}}}


def test_summarize_timeline_falling_and_verdict_flip():
    rows = [_row(60, 'HOLD', (9, 30), news=0.2),
            _row(30, 'HOLD', (11, 30), news=-0.1),
            _row(-10, 'TRIM', (14, 30), news=-0.5)]
    s = sa.summarize_timeline(rows)
    assert s['observations'] == 3
    assert s['trend_score_direction'] == 'falling'
    assert s['trend_score_change'] == -70
    assert s['latest_trend_score'] == -10
    assert s['verdict_changed'] is True
    assert s['verdict_path'] == ['HOLD', 'TRIM']
    assert s['avg_news_sentiment'] == round((0.2 - 0.1 - 0.5) / 3, 3)


def test_summarize_timeline_flat_and_unsorted_input():
    # out-of-order input; small drift within eps -> flat, verdict stable
    rows = [_row(50, 'HOLD', (14, 0)), _row(48, 'HOLD', (9, 30))]
    s = sa.summarize_timeline(rows)
    assert s['trend_score_direction'] == 'flat'
    assert s['verdict_changed'] is False
    assert s['verdict_path'] == ['HOLD']
    assert s['avg_news_sentiment'] is None     # no news present


def test_summarize_timeline_empty():
    assert sa.summarize_timeline([]) == {'observations': 0}
