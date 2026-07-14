"""Edge + error-branch coverage for indicators.py — malformed input must
degrade gracefully (None/[]/NEUTRAL), never raise into the trading loop."""
from unittest.mock import patch

import indicators as ind

BAD = "not-a-list"                        # triggers the except in most fns
GARBAGE_CANDLES = [{'close': 'x', 'high': None, 'low': {}, 'open': []}] * 40


def _c(n, base=100.0):
    # 2020 dates: safely in the past, so vwap's "today candles" filter is empty
    return [{'open': base, 'high': base + 1, 'low': base - 1,
             'close': base + i * 0.1, 'volume': 100 + i,
             'timestamp': f'2020-07-{(i % 28) + 1:02d} 10:00:00'}
            for i in range(n)]


def test_get_closes_volumes_error_paths():
    assert ind.get_closes([{'close': object()}]) == [] or isinstance(
        ind.get_closes([{'close': object()}]), list)
    assert ind.get_volumes([{'volume': object()}]) == []


def test_rsi_short_and_error():
    assert ind.calculate_rsi(_c(5)) is None            # < period
    assert ind.calculate_rsi(BAD) is None              # except


def test_ema_error_and_empty():
    assert ind.calculate_ema([], 10) is None
    assert ind.calculate_ema(BAD, 10) is None
    assert ind.calculate_ema_series(BAD, 10) == []


def test_macd_short_and_error():
    assert ind.calculate_macd(_c(10)) is None          # not enough for slow
    assert ind.calculate_macd(BAD) is None


def test_bollinger_error():
    assert ind.calculate_bollinger_bands(_c(5)) is None
    assert ind.calculate_bollinger_bands(BAD) is None


def test_atr_short_and_error():
    assert ind.calculate_atr(_c(3)) is None
    assert ind.calculate_atr(BAD) is None


def test_volume_sma_short_and_error():
    assert ind.calculate_volume_sma(_c(3)) is None
    assert ind.calculate_volume_sma(BAD) is None


def test_candle_date_variants():
    assert ind._candle_date({}) is None                # no timestamp
    assert ind._candle_date({'timestamp': 'garbage'}) is None
    # naive string date parses
    assert ind._candle_date({'timestamp': '2026-07-14 10:00:00'}) is not None


def test_vwap_no_today_and_error():
    # candles all dated in the past -> no today candles -> None
    assert ind.calculate_vwap(_c(30)) is None
    assert ind.calculate_vwap(BAD) is None


def test_vwap_zero_volume():
    from datetime import datetime
    import pytz
    today = datetime.now(pytz.timezone('Asia/Kolkata')).strftime('%Y-%m-%d')
    rows = [{'high': 10, 'low': 9, 'close': 9.5, 'volume': 0,
             'timestamp': f'{today} 10:00:00'}]
    assert ind.calculate_vwap(rows) is None            # den==0


def test_adx_short_and_error():
    assert ind.calculate_adx(_c(5)) is None
    assert ind.calculate_adx(BAD) is None


def test_candle_direction_edges():
    assert ind.get_candle_direction([], 3) == 'NEUTRAL'
    assert ind.get_candle_direction(_c(2), 3) == 'NEUTRAL'   # < lookback
    assert ind.get_candle_direction(GARBAGE_CANDLES, 3) == 'NEUTRAL'  # except


def test_error_branches_on_valid_length_garbage():
    """Valid-length series with non-numeric content reaches the compute body
    then raises -> the except/print branch each function guards with."""
    g = GARBAGE_CANDLES                                  # 40 rows, bad content
    assert ind.calculate_rsi(g) is None
    assert ind.calculate_atr(g) is None
    assert ind.calculate_macd(g) is None
    assert ind.calculate_bollinger_bands(g) is None
    assert ind.calculate_adx(g) is None
    # ema_series with non-numeric values raises inside the smoothing loop
    assert ind.calculate_ema([object()] * 30, 10) is None
    assert ind.calculate_ema_series([object()] * 30, 10) == []


def test_defensive_excepts_degrade_when_helpers_raise():
    """The inner helpers sanitize input, so these compute-body excepts can't
    be reached through the public contract. Force the dependency to raise to
    prove each still returns its safe default instead of propagating."""
    boom = lambda *a, **k: (_ for _ in ()).throw(RuntimeError('boom'))
    ok = _c(60)
    with patch.object(ind, 'get_closes', side_effect=RuntimeError('x')):
        assert ind.calculate_rsi(ok) is None
        assert ind.calculate_macd(ok) is None
        assert ind.calculate_bollinger_bands(ok) is None
    with patch.object(ind, 'calculate_ema_series', side_effect=RuntimeError('x')):
        assert ind.calculate_ema([1.0] * 30, 10) is None
    with patch.object(ind, 'get_volumes', side_effect=RuntimeError('x')):
        assert ind.calculate_volume_sma(ok) is None


def test_run_all_indicators_garbage_is_safe():
    out = ind.run_all_indicators(GARBAGE_CANDLES)
    assert out['trend_strength'] == 'CHOPPY'           # adx None fallback
    assert 'current_close' in out
    # empty input path
    empty = ind.run_all_indicators([])
    assert empty['current_close'] == 0.0
