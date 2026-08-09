"""[C5] post-close candle backfill.

The gap it closes: the in-cycle archive writes only the trailing 3 bars, and
only for symbols analyzed that cycle, so a position closing between cycles
never gets its final bars written. [P-30] measured it — 10 of 118 clean-exit
trades exit past the last archived bar, making their exit unorderable.

The property that must not regress: this runs POST-CLOSE. An archive call on
the exit path is the documented ~7s/cycle latency regression, and a slow exit
path fills stops at -2.78R instead of ~-1R.
"""
from unittest.mock import MagicMock, patch

import data_jobs


def _md(candles):
    md = MagicMock()
    md._instrument_cache = {}
    md.get_candles.return_value = candles
    return md


BARS = [
    {'timestamp': '2026-08-10T09:15:00+0530', 'open': 1, 'high': 2, 'low': 0.5, 'close': 1.5, 'volume': 10},
    {'timestamp': '2026-08-10T09:20:00+0530', 'open': 1.5, 'high': 3, 'low': 1.4, 'close': 2.9, 'volume': 20},
    {'timestamp': '2026-08-09T15:25:00+0530', 'open': 9, 'high': 9, 'low': 9, 'close': 9, 'volume': 1},
]


def test_writes_the_whole_day_not_just_the_trailing_three():
    """The trailing-3 default is the very thing that caused the gap."""
    captured = {}

    def _rows(session_id, symbol, exchange, candles, interval='5minute', tail=3):
        captured['tail'] = tail
        captured['n'] = len(candles)
        return [{'symbol': symbol}] * len(candles)

    with patch.object(data_jobs.db, 'candle_rows', side_effect=_rows), \
         patch.object(data_jobs.db, 'upsert_candles', side_effect=lambda r: len(r)), \
         patch.object(data_jobs, '_token_for', return_value=12345):
        n = data_jobs.archive_traded_day_candles(
            _md(BARS), 'sess-1', '2026-08-10', symbols=['INFY'])

    assert n == 2                 # only the two 08-10 bars
    assert captured['n'] == 2     # prior-day bar filtered out
    assert captured['tail'] == 2  # whole day, not 3


def test_filters_to_the_run_date():
    """A 3-day fetch must not write other days' bars under today's session."""
    with patch.object(data_jobs.db, 'candle_rows',
                      side_effect=lambda s, sy, e, c, interval='5minute', tail=3: list(c)), \
         patch.object(data_jobs.db, 'upsert_candles', side_effect=lambda r: len(r)), \
         patch.object(data_jobs, '_token_for', return_value=1):
        n = data_jobs.archive_traded_day_candles(
            _md(BARS), 's', '2026-08-09', symbols=['INFY'])
    assert n == 1


def test_symbol_without_a_pinned_token_is_skipped_not_fatal():
    """Post-close there is no live universe, so unpinned names simply have no
    token. One must not abort the rest of the batch."""
    with patch.object(data_jobs.db, 'candle_rows',
                      side_effect=lambda s, sy, e, c, interval='5minute', tail=3: list(c)), \
         patch.object(data_jobs.db, 'upsert_candles', side_effect=lambda r: len(r)), \
         patch.object(data_jobs, '_token_for',
                      side_effect=lambda s: None if s == 'GHOST' else 7):
        n = data_jobs.archive_traded_day_candles(
            _md(BARS), 's', '2026-08-10', symbols=['GHOST', 'INFY'])
    assert n == 2  # INFY still archived


def test_one_bad_symbol_does_not_take_down_the_batch():
    md = _md(BARS)
    md.get_candles.side_effect = [Exception('boom'), BARS]
    with patch.object(data_jobs.db, 'candle_rows',
                      side_effect=lambda s, sy, e, c, interval='5minute', tail=3: list(c)), \
         patch.object(data_jobs.db, 'upsert_candles', side_effect=lambda r: len(r)), \
         patch.object(data_jobs, '_token_for', return_value=1):
        n = data_jobs.archive_traded_day_candles(
            md, 's', '2026-08-10', symbols=['BAD', 'INFY'])
    assert n == 2


def test_expired_token_stops_the_batch():
    """Every subsequent symbol would fail the same way — 80 doomed calls is a
    silent stall, which is exactly what REQ-083 exists to prevent."""
    from kite_client import TokenExpiredError
    md = _md(BARS)
    md.get_candles.side_effect = TokenExpiredError('dead')
    with patch.object(data_jobs.db, 'candle_rows', return_value=[]), \
         patch.object(data_jobs.db, 'upsert_candles', return_value=0), \
         patch.object(data_jobs, '_token_for', return_value=1):
        n = data_jobs.archive_traded_day_candles(
            md, 's', '2026-08-10', symbols=['A', 'B', 'C'])
    assert n == 0
    assert md.get_candles.call_count == 1   # stopped, did not grind through


def test_no_trades_is_a_clean_noop():
    with patch.object(data_jobs.db, 'traded_symbols_on', return_value=[]):
        assert data_jobs.archive_traded_day_candles(
            _md(BARS), 's', '2026-08-10') == 0


def test_defaults_to_the_days_traded_symbols():
    with patch.object(data_jobs.db, 'traded_symbols_on',
                      return_value=['INFY']) as ts, \
         patch.object(data_jobs.db, 'candle_rows',
                      side_effect=lambda s, sy, e, c, interval='5minute', tail=3: list(c)), \
         patch.object(data_jobs.db, 'upsert_candles', side_effect=lambda r: len(r)), \
         patch.object(data_jobs, '_token_for', return_value=1):
        data_jobs.archive_traded_day_candles(_md(BARS), 's', '2026-08-10')
    ts.assert_called_once_with('2026-08-10')


# --- the scheduler gate ------------------------------------------------------

def _at(hh, mm):
    """Patch scheduler's clock to a Monday at hh:mm IST."""
    import scheduler
    d = MagicMock()
    d.weekday.return_value = 0
    d.hour, d.minute = hh, mm
    d.date.return_value.isoformat.return_value = '2026-08-10'
    return patch.object(scheduler, 'datetime',
                        MagicMock(now=MagicMock(return_value=d)))


def test_gate_is_shut_during_market_hours():
    """The whole point is that this never runs while trading is live."""
    import scheduler
    scheduler._candle_backfill_days.clear()
    with _at(11, 0), patch.object(scheduler.db, 'get_enc_token') as tok:
        scheduler._maybe_backfill_candles()
    tok.assert_not_called()


def test_gate_opens_post_close_and_runs_once_per_day():
    import scheduler
    scheduler._candle_backfill_days.clear()
    scheduler._candle_backfill_inflight.clear()
    with _at(15, 45), \
         patch.object(scheduler.db, 'get_enc_token', return_value='t'), \
         patch.object(scheduler, '_token_is_live', return_value=True), \
         patch.object(scheduler.threading, 'Thread') as th:
        scheduler._maybe_backfill_candles()
        assert th.call_count == 1
        # simulate the thread having succeeded
        scheduler._candle_backfill_days.add('2026-08-10')
        scheduler._maybe_backfill_candles()
        assert th.call_count == 1   # not run twice
