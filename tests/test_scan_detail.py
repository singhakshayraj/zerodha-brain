"""The universe scan must persist a full verdict, not just a score. [2026-08-24]

A bare trend score is not an analysis -- the useful parts are the reasons, the
bear case and the levels. advise() is pure and the scan already holds the
candles, so it produces them there rather than via an on-demand path that would
need a live token and re-fetch the same data.
"""


import advisor_scoring


def _candles(n=260, start=100.0, step=0.4):
    """A clean uptrend -- enough completed bars to clear MIN_DAILY_BARS."""
    out, p = [], start
    for i in range(n):
        o = p; p = round(p + step, 2)
        out.append({'open': o, 'high': max(o, p) + 0.5, 'low': min(o, p) - 0.5,
                    'close': p, 'volume': 100000,
                    'timestamp': '2026-01-01T00:00:00+0530'})
    return out


def test_advise_works_for_a_stock_you_do_not_own():
    """qty 0 / avg None is the non-held case. It must produce a real verdict,
    not crash and not fabricate position economics."""
    c = _candles()
    out = advisor_scoring.advise(
        {'symbol': 'TESTCO', 'quantity': 0, 'average_price': None,
         'last_price': c[-1]['close']}, c)

    assert out['verdict'] in ('HOLD', 'TRIM', 'SELL', 'SELL_ON_BOUNCE', 'INSUFFICIENT')
    assert isinstance(out['reasons'], list) and out['reasons']
    assert 'counter_case' in out                 # [P-33] the bear case
    assert 'trend_score' in out and 'indicators' in out
    # Not owned -> no P&L to report. Absent, not zero-as-if-flat.
    assert out['pnl_percent'] is None


def test_detail_is_json_serialisable():
    """It is stored in a jsonb column, so anything non-serialisable silently
    kills the whole bulk upsert."""
    import json
    c = _candles()
    out = advisor_scoring.advise(
        {'symbol': 'TESTCO', 'quantity': 0, 'average_price': None,
         'last_price': c[-1]['close']}, c)
    json.dumps(out)     # raises if not
