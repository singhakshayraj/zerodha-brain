"""M3 data-layer + REQ-050 step-0 tests: data-quality quarantine, level pack,
stock profile, in-play ranking. Pure modules — no DB, no network."""
import os
from unittest.mock import MagicMock, patch

with patch.dict(os.environ, {
    'SUPABASE_URL': 'https://fake.supabase.co',
    'SUPABASE_SERVICE_KEY': 'fake-key',
}):
    with patch('supabase.create_client', return_value=MagicMock()):
        import database  # noqa

import config
import data_quality as dq
import inplay
import level_pack as lp
import stock_profile as sp


def _c(o, h, l, cl, v=1000, ts='2026-07-07T09:15:00+0530'):
    return {'open': o, 'high': h, 'low': l, 'close': cl, 'volume': v,
            'timestamp': ts}


# --- REQ-050 step 0: data-quality quarantine ---

def test_dq_ok_normal_quote():
    ok, reason = dq.check_quote(100.0, last_candle_close=101.0)
    assert ok is True and reason is None


def test_dq_no_price_quarantined():
    ok, reason = dq.check_quote(0, last_candle_close=100)
    assert ok is False and reason == 'QUARANTINE_NO_PRICE'
    assert dq.check_quote(None)[1] == 'QUARANTINE_NO_PRICE'


def test_dq_nonfinite_quarantined():
    assert dq.check_quote(float('inf'), 100)[1] == 'QUARANTINE_NONFINITE'
    assert dq.check_quote(float('nan'), 100)[1] == 'QUARANTINE_NONFINITE'


def test_dq_stale_quarantined():
    ok, reason = dq.check_quote(100, quote_age_s=999, max_stale_s=600)
    assert ok is False and reason == 'QUARANTINE_STALE'


def test_dq_price_deviation_quarantined():
    # 50% away from the last candle close → wrong token / bad quote
    ok, reason = dq.check_quote(150, last_candle_close=100)
    assert ok is False and reason == 'QUARANTINE_PRICE_DEVIATION'


def test_dq_small_deviation_ok():
    ok, _ = dq.check_quote(105, last_candle_close=100)  # 5% — fine
    assert ok is True


def test_dq_no_candle_reference_still_passes_live_price():
    ok, _ = dq.check_quote(100, last_candle_close=None)
    assert ok is True


# --- level pack ---

def _daily_series(n=20, start=100.0):
    out = []
    for i in range(n):
        base = start + i
        out.append({'date': f'2026-06-{i+1:02d}', 'open': base,
                    'high': base + 2, 'low': base - 2, 'close': base + 0.5,
                    'volume': 1000 + i})
    return out


def test_prior_day_levels():
    daily = _daily_series(3)
    lv = lp.prior_day_levels(daily)
    assert lv['pdh'] == daily[-1]['high']
    assert lv['pdc'] == daily[-1]['close']


def test_round_levels_scale_with_price():
    assert 100 in lp.round_levels(101)
    assert all(l > 0 for l in lp.round_levels(12.4))
    assert lp.round_levels(0) == []


def test_weekly_high_low():
    daily = _daily_series(10)
    wk = lp.weekly_high_low(daily, sessions=5)
    assert wk['weekly_high'] == max(d['high'] for d in daily[-5:])


def test_daily_ohlc_collapses_intraday():
    intraday = [
        _c(10, 12, 9, 11, ts='2026-07-06T09:15:00+0530'),
        _c(11, 14, 10, 13, ts='2026-07-06T09:30:00+0530'),
        _c(13, 15, 12, 14, ts='2026-07-07T09:15:00+0530'),
    ]
    daily = lp.daily_ohlc(intraday)
    assert len(daily) == 2
    assert daily[0]['open'] == 10 and daily[0]['high'] == 14 and daily[0]['close'] == 13


def test_level_pack_build_shape():
    row = lp.build('NSE:INFY', '2026-07-07', _daily_series(20))
    assert row['symbol'] == 'NSE:INFY'
    assert row['pdh'] is not None and row['weekly_high'] is not None
    assert isinstance(row['round_levels'], list)


def test_gap_levels_direction():
    daily = [{'date': 'd1', 'open': 100, 'high': 101, 'low': 99, 'close': 100},
             {'date': 'd2', 'open': 103, 'high': 104, 'low': 102, 'close': 103}]
    g = lp.gap_levels(daily)
    assert g['direction'] == 'UP' and g['gap_pct'] == 3.0


# --- stock profile ---

def test_efficiency_ratio_trend_vs_chop():
    trend = [100, 101, 102, 103, 104]      # straight up → ~1.0
    chop = [100, 105, 100, 105, 100]       # zigzag → low
    assert sp.efficiency_ratio(trend) > sp.efficiency_ratio(chop)


def test_gap_follow_rate():
    daily = [
        {'open': 100, 'close': 100},
        {'open': 102, 'close': 104},   # gap up, followed
        {'open': 103, 'close': 101},   # gap down (prev close 104), followed
    ]
    r = sp.gap_follow_rate(daily)
    assert r['samples'] == 2 and r['rate'] == 1.0


def test_profile_falls_back_to_universe_avg_when_thin():
    thin = _daily_series(5)  # < MIN_SAMPLES
    row = sp.build('NSE:X', '2026-07-07', thin, 90,
                   universe_avg={'trendiness': 0.42, 'gap_follow_rate': 0.55})
    assert row['sample_sizes']['fell_back_to_universe_avg'] is True
    # thin series still yields its own efficiency ratio (>=2 closes), but a
    # thin gap sample with no gaps borrows the universe rate
    assert row['gap_follow_rate'] in (0.55, sp.gap_follow_rate(thin)['rate'])


def test_profile_no_fallback_when_enough_history():
    row = sp.build('NSE:Y', '2026-07-07', _daily_series(40), 90,
                   universe_avg={'trendiness': 0.42})
    assert row['sample_sizes']['fell_back_to_universe_avg'] is False


# --- in-play ranking ---

def test_opening_range_rvol():
    assert inplay.opening_range_rvol(300, 100) == 3.0
    assert inplay.opening_range_rvol(300, 0) is None


def test_rank_filters_and_caps():
    cands = [
        {'symbol': 'A', 'or_rvol': 5.0},
        {'symbol': 'B', 'or_rvol': 1.0},   # below threshold 2.0
        {'symbol': 'C', 'or_rvol': 3.0},
        {'symbol': 'D', 'or_rvol': None},  # unknown → excluded
    ]
    ranked = inplay.rank(cands, cap=10, min_rvol=2.0)
    syms = [r['symbol'] for r in ranked]
    assert syms == ['A', 'C']              # sorted desc, B and D dropped
    assert ranked[0]['rank'] == 1 and ranked[1]['rank'] == 2


def test_rank_respects_cap():
    cands = [{'symbol': s, 'or_rvol': float(i + 3)}
             for i, s in enumerate('ABCDE')]
    ranked = inplay.rank(cands, cap=2, min_rvol=2.0)
    assert len(ranked) == 2
    assert ranked[0]['or_rvol'] >= ranked[1]['or_rvol']
