"""Advisor accountability backtest (Phase 3). ADVISORY ONLY — pins no order
path is touched."""
import os
from unittest.mock import MagicMock, patch

with patch.dict(os.environ, {
    'SUPABASE_URL': 'https://fake.supabase.co',
    'SUPABASE_SERVICE_KEY': 'fake-key',
}):
    with patch('supabase.create_client', return_value=MagicMock()):
        import database  # noqa

import advisor_backtest as bt


# --- evaluate_verdict (pure) ---------------------------------------------------

def test_hold_correct_when_price_rose():
    out = bt.evaluate_verdict({'verdict': 'HOLD'}, 5.0, 2.0)
    assert out['outcome_correct'] is True
    assert out['outcome_return_pct'] == 5.0
    assert out['outcome_vs_nifty_pct'] == 3.0


def test_hold_incorrect_when_price_fell():
    assert bt.evaluate_verdict({'verdict': 'HOLD'}, -4.0)['outcome_correct'] is False


def test_sell_correct_when_price_fell():
    for v in ('SELL', 'SELL_ON_BOUNCE', 'TRIM'):
        assert bt.evaluate_verdict({'verdict': v}, -6.0)['outcome_correct'] is True


def test_sell_incorrect_when_price_rose():
    assert bt.evaluate_verdict({'verdict': 'SELL'}, 8.0)['outcome_correct'] is False


def test_insufficient_is_never_judged():
    assert bt.evaluate_verdict({'verdict': 'INSUFFICIENT'}, 5.0)['outcome_correct'] is None


def test_no_nifty_means_null_alpha():
    assert bt.evaluate_verdict({'verdict': 'HOLD'}, 5.0)['outcome_vs_nifty_pct'] is None


# --- run_backtest_pass -----------------------------------------------------------

def _bars(dates_closes):
    return [{'timestamp': d, 'close': c} for d, c in dates_closes]


def _md(symbol_bars, nifty_bars=None):
    md = MagicMock()
    md._instrument_cache = {}

    def candles(key, interval, days):
        if key == 'NSE:NIFTY 50':
            return nifty_bars or []
        return symbol_bars

    md.get_candles.side_effect = candles
    return md


_TEN = _bars([(f'2026-07-{d:02d}', 100 - d) for d in range(1, 12)])  # falling


def test_backtest_evaluates_due_row_and_skips_undue():
    rows = [
        {'run_date': '2026-07-01', 'symbol': 'AAA', 'verdict': 'SELL',
         'last_price': 100.0},
        {'run_date': '2026-07-10', 'symbol': 'AAA', 'verdict': 'SELL',
         'last_price': 90.0},          # only 2 bars after — not due
    ]
    written = {}
    with patch.object(bt.db, 'get_unevaluated_advice', return_value=rows), \
         patch.object(bt.db, 'update_advice_outcome',
                      side_effect=lambda d, s, o: written.setdefault((d, s), o) or True):
        n = bt.run_backtest_pass(_md(_TEN), horizon_days=10)
    assert n == 1
    out = written[('2026-07-01', 'AAA')]
    # bar[9] after 07-01 is 07-10 close=90 -> -10% -> SELL correct
    assert out['outcome_return_pct'] == -10.0
    assert out['outcome_correct'] is True
    assert out['evaluated_at']


def test_backtest_insufficient_rows_leave_queue_unjudged():
    rows = [{'run_date': '2026-07-01', 'symbol': 'BBB',
             'verdict': 'INSUFFICIENT', 'last_price': 0}]
    written = {}
    with patch.object(bt.db, 'get_unevaluated_advice', return_value=rows), \
         patch.object(bt.db, 'update_advice_outcome',
                      side_effect=lambda d, s, o: written.setdefault((d, s), o) or True):
        assert bt.run_backtest_pass(_md([]), horizon_days=10) == 1
    assert written[('2026-07-01', 'BBB')]['outcome_correct'] is None


def test_backtest_per_row_failure_isolated_and_no_order_path():
    rows = [
        {'run_date': '2026-07-01', 'symbol': 'BAD', 'verdict': 'SELL',
         'last_price': 100.0},
        {'run_date': '2026-07-01', 'symbol': 'AAA', 'verdict': 'SELL',
         'last_price': 100.0},
    ]
    md = MagicMock()
    md._instrument_cache = {}

    def candles(key, interval, days):
        if 'BAD' in key:
            raise Exception('boom')
        if key == 'NSE:NIFTY 50':
            return []
        return _TEN

    md.get_candles.side_effect = candles
    with patch.object(bt.db, 'get_unevaluated_advice', return_value=rows), \
         patch.object(bt.db, 'update_advice_outcome', return_value=True):
        assert bt.run_backtest_pass(md, horizon_days=10) == 1
    for name in ('place_buy_order', 'place_sell_order', 'place_order'):
        assert not getattr(md.kite, name).called


def test_backtest_empty_queue_noop():
    md = MagicMock()
    with patch.object(bt.db, 'get_unevaluated_advice', return_value=[]):
        assert bt.run_backtest_pass(md, horizon_days=10) == 0
    md.get_candles.assert_not_called()


# --- track-record summary ---------------------------------------------------------

def test_track_record_summary_aggregates():
    rows = [
        {'verdict': 'SELL', 'outcome_correct': True, 'outcome_return_pct': -10.0,
         'outcome_vs_nifty_pct': -11.0, 'quantity': 10, 'last_price': 100.0},
        {'verdict': 'HOLD', 'outcome_correct': True, 'outcome_return_pct': 6.0,
         'outcome_vs_nifty_pct': 4.0, 'quantity': 5, 'last_price': 50.0},
        {'verdict': 'TRIM', 'outcome_correct': False, 'outcome_return_pct': 4.0,
         'outcome_vs_nifty_pct': 2.0, 'quantity': 20, 'last_price': 10.0},
        {'verdict': 'INSUFFICIENT', 'outcome_correct': None},   # never judged
    ]
    with patch.object(bt.db, 'get_evaluated_advice', return_value=rows):
        s = bt.get_track_record_summary()
    assert s['evaluated_calls'] == 3
    assert s['hit_rate_pct'] == 66.7
    assert s['avg_return_pct'] == 0.0            # (-10+6+4)/3
    assert s['avg_alpha_pct'] == -1.67
    # SELL saved 10*100*10% = +100; TRIM cost 0.5*20*10*4% = -4; HOLD 0
    assert s['advice_value_inr'] == 96.0
    assert s['by_verdict']['SELL'] == {'calls': 1, 'hits': 1}
    assert s['by_verdict']['TRIM'] == {'calls': 1, 'hits': 0}


def test_track_record_summary_empty():
    with patch.object(bt.db, 'get_evaluated_advice', return_value=[]):
        s = bt.get_track_record_summary()
    assert s['evaluated_calls'] == 0 and s['advice_value_inr'] == 0.0


# --- factor_attribution (pure) -------------------------------------------------

def _row(correct, alpha, *, price=100.0, ema200=None, ema50=None, rsi=None,
         adx=None, consistency=None, rel=None, vol=None, trigger=None):
    return {
        'outcome_correct': correct, 'outcome_vs_nifty_pct': alpha,
        'last_price': price, 'trigger_type': trigger,
        'indicators': {'ema_200': ema200, 'ema_50': ema50, 'rsi_14': rsi,
                       'adx': adx, 'trend_consistency_pct': consistency,
                       'relative_strength_vs_nifty': rel, 'volume_trend_ratio': vol},
    }


def test_factor_attribution_separates_a_predictive_factor():
    # ema200_position perfectly predicts: above-200EMA always right,
    # below-200EMA always wrong. min_bucket_n=2 so both buckets rank.
    rows = (
        [_row(True, 3.0, price=110, ema200=100) for _ in range(3)]
        + [_row(False, -3.0, price=90, ema200=100) for _ in range(3)]
    )
    a = bt.factor_attribution(rows, min_bucket_n=2)
    assert a['graded_calls'] == 6
    f = a['factors']['ema200_position']
    assert f['buckets']['price_above_200EMA']['hit_rate_pct'] == 100.0
    assert f['buckets']['price_below_200EMA']['hit_rate_pct'] == 0.0
    assert f['separation_pct'] == 100.0
    assert a['ranked_by_separation'][0] == 'ema200_position'


def test_factor_attribution_ignores_missing_data_and_flags_low_n():
    # rsi present on only one row -> its single bucket is low-n, unrankable.
    rows = [
        _row(True, 1.0, rsi=25),                        # oversold
        _row(True, 1.0, ema200=None, ema50=None),       # rsi absent -> skipped for rsi
    ]
    a = bt.factor_attribution(rows, min_bucket_n=5)
    rsi = a['factors']['rsi_zone']
    assert rsi['buckets']['oversold']['n'] == 1
    assert rsi['buckets']['oversold']['low_n'] is True
    assert rsi['separation_pct'] is None          # <2 sufficiently-sampled buckets
    assert 'rsi_zone' not in a['ranked_by_separation']


def test_factor_attribution_empty():
    a = bt.factor_attribution([])
    assert a['graded_calls'] == 0
    assert a['ranked_by_separation'] == []


def test_factor_attribution_skips_ungraded_rows():
    rows = [_row(None, None, price=110, ema200=100),   # INSUFFICIENT-style, excluded
            _row(True, 2.0, price=110, ema200=100)]
    a = bt.factor_attribution(rows, min_bucket_n=1)
    assert a['graded_calls'] == 1
