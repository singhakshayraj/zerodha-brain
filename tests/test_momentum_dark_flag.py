"""12-1 momentum is logged but must NOT score. [factor_lab]

scripts/factor_lab.py tested the standard cross-sectional factor set over 5.2
years; nothing survived Holm correction out of sample. mom_12_1 was the only
factor to keep a positive sign in explore AND holdout across all four cuts,
so it is logged to earn a live track record -- and deliberately kept out of
the numeric score until factor_attribution grades it.

The test that matters is the last one: if mom_12_1 ever starts moving
trend_score, an unproven factor has quietly begun trading real money.
"""
from datetime import datetime, timedelta

from advisor_scoring import advise, momentum_12_1, trend_score


def _series(n=300, step=1.0, start=100.0):
    return [start + step * i for i in range(n)]


def _candles(closes):
    day = datetime(2026, 1, 1) - timedelta(days=len(closes) + 1)
    out = []
    for i, c in enumerate(closes):
        d = day + timedelta(days=i)
        out.append({'open': c, 'high': c * 1.01, 'low': c * 0.99, 'close': c,
                    'volume': 100000,
                    'timestamp': d.strftime('%Y-%m-%dT00:00:00+0530')})
    return out


def test_skips_the_most_recent_month():
    """The skip window is the whole point -- the last 21 days are short-term
    reversal territory, not momentum."""
    closes = [100.0] * 300
    for i in range(len(closes) - 21, len(closes)):
        closes[i] = 1000.0
    assert momentum_12_1(closes) == 0.0


def test_reads_the_12_month_window():
    closes = [100.0] * 300
    closes[-253] = 50.0            # start of the 12-1 window
    assert momentum_12_1(closes) == 100.0


def test_none_without_enough_history():
    assert momentum_12_1([100.0] * 252) is None
    assert momentum_12_1([]) is None


def test_logged_on_the_advice_row():
    adv = advise({'symbol': 'TEST', 'quantity': 10, 'average_price': 100.0,
                  'last_price': 400.0}, _candles(_series()))
    assert 'mom_12_1' in adv['indicators']
    assert adv['indicators']['mom_12_1'] is not None


def test_does_not_move_the_score():
    """Dark-flag guard: trend_score must ignore mom_12_1 entirely."""
    closes = _series()
    ind = {'current_close': closes[-1], 'ema_200': 200.0, 'ema_50': 300.0,
           'adx': 30.0, 'adx_plus_di': 25.0, 'adx_minus_di': 10.0}
    without = trend_score(ind, closes, consistency=80.0, rel_strength=5.0)
    for spike in (-99.0, 0.0, 250.0):
        seeded = dict(ind, mom_12_1=spike)
        assert trend_score(seeded, closes, consistency=80.0,
                           rel_strength=5.0) == without
