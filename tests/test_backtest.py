from datetime import datetime

import pytz

import backtest

IST = pytz.timezone('Asia/Kolkata')


def _bar(day, hh, mm, o, h, l, c, vol=100000):
    return {
        'timestamp': f'{day}T{hh:02d}:{mm:02d}:00+05:30',
        'open': o, 'high': h, 'low': l, 'close': c, 'volume': vol,
    }


def _ts(day, hh, mm):
    return IST.localize(datetime.strptime(f'{day} {hh:02d}:{mm:02d}', '%Y-%m-%d %H:%M'))


# --- classify_regime_period ---

def test_classify_regime_period_boundaries():
    assert backtest.classify_regime_period('2020-03-15') == 'CRASH_2020'
    assert backtest.classify_regime_period('2021-06-01') == 'BULL_2021'
    assert backtest.classify_regime_period('2022-11-01') == 'CHOP_2022'
    assert backtest.classify_regime_period('2026-07-22') is None
    assert backtest.classify_regime_period('2020-01-31') is None  # day before window


# --- _walk_forward_exit ---

def test_walk_forward_long_stop_hit_before_target():
    day = '2026-01-05'
    candles = [
        _bar(day, 9, 40, 100, 101, 99.5, 100.5),
        _bar(day, 9, 45, 100.5, 101, 97, 97.5),   # dips through stop=98
        _bar(day, 9, 50, 97.5, 110, 97, 105),     # would've hit target too, later
    ]
    price, reason, ts = backtest._walk_forward_exit(
        'LONG', entry=100, stop=98, target=106, candles_after=candles,
        entry_ts=_ts(day, 9, 35), exit_style='FIXED_TARGET', time_stop_min=999)
    assert reason == 'STOP_HIT'
    assert price == 98


def test_walk_forward_short_target_hit():
    day = '2026-01-05'
    candles = [
        _bar(day, 9, 40, 100, 100.5, 98, 98.5),
        _bar(day, 9, 45, 98.5, 99, 94, 94.5),   # crosses target=95 (short profits going down)
    ]
    price, reason, ts = backtest._walk_forward_exit(
        'SHORT', entry=100, stop=103, target=95, candles_after=candles,
        entry_ts=_ts(day, 9, 35), exit_style='FIXED_TARGET', time_stop_min=999)
    assert reason == 'TARGET_HIT'
    assert price == 95


def test_walk_forward_session_end_square_off():
    day = '2026-01-05'
    candles = [
        _bar(day, 9, 40, 100, 101, 99.5, 100.5),
        _bar(day, 15, 20, 100.5, 101, 100, 100.8),  # square-off cutoff
    ]
    price, reason, ts = backtest._walk_forward_exit(
        'LONG', entry=100, stop=90, target=200, candles_after=candles,
        entry_ts=_ts(day, 9, 35), exit_style='FIXED_TARGET', time_stop_min=999)
    assert reason == 'SESSION_END'
    assert price == 100.8


def test_walk_forward_time_stop_scratches_flat_position():
    day = '2026-01-05'
    # entry at 10:00, time_stop_min=40 -> cutoff 10:40. Price barely moves.
    candles = [
        _bar(day, 10, 5, 100, 100.3, 99.8, 100.1),
        _bar(day, 10, 40, 100.1, 100.4, 99.9, 100.2),
    ]
    price, reason, ts = backtest._walk_forward_exit(
        'LONG', entry=100, stop=98, target=110, candles_after=candles,
        entry_ts=_ts(day, 10, 0), exit_style='FIXED_TARGET', time_stop_min=40)
    assert reason == 'TIME_STOP'
    assert price == 100.2


def test_walk_forward_time_stop_does_not_fire_on_meaningful_move():
    day = '2026-01-05'
    # entry at 10:00, stop=98 (risk=2), by 10:40 price is +1 (0.5R) >= 0.3R threshold
    candles = [
        _bar(day, 10, 40, 100, 101.2, 100.5, 101.0),
    ]
    price, reason, ts = backtest._walk_forward_exit(
        'LONG', entry=100, stop=98, target=110, candles_after=candles,
        entry_ts=_ts(day, 10, 0), exit_style='FIXED_TARGET', time_stop_min=40)
    assert reason != 'TIME_STOP'


def test_walk_forward_trail_to_close_ratchets_up_and_exits_on_pullback():
    day = '2026-01-05'
    candles = [
        _bar(day, 9, 40, 100, 105, 100, 104),   # trail moves up: 105 - 2 = 103
        _bar(day, 9, 45, 104, 110, 103.5, 109),  # trail moves up: 110 - 2 = 108
        _bar(day, 9, 50, 109, 109, 107, 107.5),  # pulls back through trail (108)
    ]
    price, reason, ts = backtest._walk_forward_exit(
        'LONG', entry=100, stop=98, target=200, candles_after=candles,
        entry_ts=_ts(day, 9, 35), exit_style='TRAIL_TO_CLOSE', time_stop_min=999)
    assert reason == 'TRAIL_STOP_HIT'
    assert price == 108  # trailed up from initial stop 98, never gives it all back


# --- aggregation ---

def test_profit_factor_and_drawdown():
    trades = [
        {'pnl': 100, 'entry_ts': '2026-01-01T09:40:00+05:30'},
        {'pnl': -40, 'entry_ts': '2026-01-01T10:00:00+05:30'},
        {'pnl': -80, 'entry_ts': '2026-01-01T10:30:00+05:30'},
        {'pnl': 50, 'entry_ts': '2026-01-01T11:00:00+05:30'},
    ]
    assert backtest._profit_factor(trades) == round(150 / 120, 3)
    # equity path: +100 (peak 100) -> +60 -> -20 (dd=120) -> +30
    assert backtest._max_drawdown(trades) == 120.0


def test_profit_factor_none_when_no_losses():
    trades = [{'pnl': 50, 'entry_ts': 'x'}, {'pnl': 30, 'entry_ts': 'y'}]
    assert backtest._profit_factor(trades) is None


# --- end-to-end smoke test via ORB (deterministic, doesn't need signal_engine's
# full confluence scoring to trigger — that engine has its own test suite) ---

def test_simulate_symbol_day_orb_end_to_end():
    day = '2026-01-05'
    def _breakout_bar(i, price):
        total_min = 30 + 5 * i
        return _bar(day, 9 + total_min // 60, total_min % 60,
                   price, price + 1, price - 0.2, price + 0.5, vol=300000)

    candles = (
        [_bar(day, 9, 15, 100, 101, 99.5, 100.5),
         _bar(day, 9, 20, 100.5, 101, 100, 100.8),
         _bar(day, 9, 25, 100.8, 101, 100.2, 100.9)]   # opening range: 99.5-101
        + [_breakout_bar(i, 101 + i) for i in range(12)]  # decisive breakout above OR high with volume
        + [_bar(day, 15, 20, 113, 114, 112, 113.5)]
    )
    trades = backtest.simulate_symbol_day(
        'TESTSTOCK', candles, [], [], [],
        entry_archetype='ORB', exit_style='FIXED_TARGET', time_stop_min=999,
        capital=25000.0,
    )
    assert isinstance(trades, list)
    for t in trades:
        assert t['archetype'] == 'ORB'
        assert t['quantity'] > 0
        assert t['regime_period'] is None  # 2026 isn't one of the three test windows
        assert t['r_multiple'] is not None


def test_run_backtest_aggregates_across_symbols():
    day = '2026-01-05'

    def _breakout_bar(i, price):
        total_min = 30 + 5 * i
        return _bar(day, 9 + total_min // 60, total_min % 60,
                   price, price + 1, price - 0.2, price + 0.5, vol=300000)

    candles = (
        [_bar(day, 9, 15, 100, 101, 99.5, 100.5),
         _bar(day, 9, 20, 100.5, 101, 100, 100.8),
         _bar(day, 9, 25, 100.8, 101, 100.2, 100.9)]
        + [_breakout_bar(i, 101 + i) for i in range(12)]
        + [_bar(day, 15, 20, 113, 114, 112, 113.5)]
    )
    result = backtest.run_backtest(
        symbol_days={'A': {day: {'5minute': candles, '15minute': [], '60minute': []}}},
        nifty_days={day: {'5minute': []}},
        entry_archetype='ORB', exit_style='FIXED_TARGET', time_stop_min=999,
    )
    assert 'profit_factor' in result
    assert 'max_drawdown' in result
    assert result['entry_archetype'] == 'ORB'
    assert result['n'] == len(result['trades'])
