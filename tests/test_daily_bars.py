"""Daily-bar sync: keeps Nifty-500 + Nifty 50 daily bars in `candles`
(interval='day') topped up from a watermark, completed bars only."""
from datetime import datetime
from unittest.mock import patch

import pytz

import data_jobs
from kite_client import TokenExpiredError

IST = pytz.timezone('Asia/Kolkata')


def _bar(d, close=100.0):
    return {'timestamp': f'{d}T00:00:00+0530', 'open': close, 'high': close,
            'low': close, 'close': close, 'volume': 10}


class FakeMD:
    def __init__(self, bars, fail=None):
        self.bars, self.fail = bars, fail or {}
        self._instrument_cache = {}

    def get_candles(self, key, interval, days):
        assert interval == 'day'
        if key in self.fail:
            raise self.fail[key]
        return self.bars


def test_last_completed_trading_day():
    f = data_jobs.last_completed_trading_day
    # Thu 11-12 before close -> Wed 11-11; after close -> same day
    assert f(IST.localize(datetime(2026, 11, 12, 10, 0))) == '2026-11-11'
    # Wed 11-11 morning skips the 11-10 Diwali holiday -> Mon 11-09
    assert f(IST.localize(datetime(2026, 11, 11, 10, 0))) == '2026-11-09'
    assert f(IST.localize(datetime(2026, 11, 11, 15, 45))) == '2026-11-11'
    # Monday morning -> previous Friday
    assert f(IST.localize(datetime(2026, 11, 16, 9, 0))) == '2026-11-13'
    # Saturday afternoon -> Friday
    assert f(IST.localize(datetime(2026, 11, 14, 16, 0))) == '2026-11-13'


def _run(md, through='2026-11-05', now=(2026, 11, 12, 10, 0), symbols=2):
    tokens = {f'NSE:S{i}': i + 1 for i in range(symbols)}
    written, cfg = [], {}
    with patch.object(data_jobs.config, 'NIFTY500_INSTRUMENT_TOKENS', tokens), \
         patch.object(data_jobs.db, 'get_config', return_value=through), \
         patch.object(data_jobs.db, 'write_config',
                      side_effect=lambda k, v: cfg.__setitem__(k, v)), \
         patch.object(data_jobs.db, 'upsert_candles',
                      side_effect=lambda rows: written.extend(rows) or len(rows)):
        n = data_jobs.sync_daily_bars(md, now_ist=IST.localize(datetime(*now)))
    return n, written, cfg


def test_writes_only_new_completed_bars_and_advances_watermark():
    bars = [_bar(d) for d in ('2026-11-05', '2026-11-06', '2026-11-11', '2026-11-12')]
    n, written, cfg = _run(FakeMD(bars))
    # 3 instruments (index + 2 stocks) x 2 new completed days (11-06, 11-11);
    # 11-05 already stored, 11-12 still forming at 10:00
    assert n == 6
    assert {r['trade_date'] for r in written} == {'2026-11-06', '2026-11-11'}
    assert {r['interval'] for r in written} == {'day'}
    assert 'NIFTY 50' in {r['symbol'] for r in written}
    assert cfg == {data_jobs.DAILY_BARS_KEY: '2026-11-11'}


def test_already_current_does_nothing():
    n, written, cfg = _run(FakeMD([_bar('2026-11-11')]), through='2026-11-11')
    assert (n, written, cfg) == (0, [], {})


def test_dead_token_keeps_watermark():
    md = FakeMD([_bar('2026-11-11')], fail={'NSE:S0': TokenExpiredError('x')})
    n, written, cfg = _run(md)
    assert cfg == {}            # retried on the next live token


def test_many_failures_keep_watermark():
    md = FakeMD([], fail={})    # every instrument returns no bars
    n, written, cfg = _run(md, symbols=20)
    assert n == 0 and cfg == {}
