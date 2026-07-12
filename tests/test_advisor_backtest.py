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
