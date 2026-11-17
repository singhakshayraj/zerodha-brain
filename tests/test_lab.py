from datetime import datetime, timedelta
from unittest.mock import patch

import pytz

import lab
import lab_db
from kite_client import TokenExpiredError
from tests.fake_supabase import FakeSupabase

IST = pytz.timezone('Asia/Kolkata')
TODAY = '2026-11-19'


def _bar(ts, close, o=None, l=None):
    o = close if o is None else o
    return {'timestamp': f'{ts}:00+0530', 'open': o, 'high': max(o, close) + 0.5,
            'low': min(o, close) if l is None else l, 'close': close, 'volume': 1000}


GAP_BARS = [_bar('2026-11-18T15:25', 100.0)] + [_bar(f'{TODAY}T{t}', 102.0, o=102.0, l=101.0)
                                               for t in ('09:15', '09:20', '09:25')]


class FakeKite:
    def __init__(self, ltp=102.0):
        self.ltp, self.fail, self.hist_calls = ltp, False, 0

    def get_historical_data(self, token, interval, from_date, to_date):
        if self.fail:
            raise TokenExpiredError('expired')
        self.hist_calls += 1
        return [dict(b) for b in GAP_BARS]

    def get_ltp(self, symbols):
        if self.fail:
            raise TokenExpiredError('expired')
        return {s: {'last_price': self.ltp} for s in symbols}


class Clock:
    def __init__(self, hms):
        self.t = IST.localize(datetime.strptime(f'{TODAY} {hms}', '%Y-%m-%d %H:%M:%S'))

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += timedelta(seconds=s)


def _runner(f, kite=None, clock=None, should_yield=lambda: False):
    clock = clock or Clock('09:30:30')
    return lab.LabRunner(kite or FakeKite(), TODAY, should_yield, clock=clock,
                         universe=[('AAA', 1, 1)], sleep=clock.sleep)


def _open(f):
    return [r for r in f.tables.get('lab_trades', []) if r['status'] == 'OPEN']


def test_cycle_opens_and_eod_closes_positions():
    f = FakeSupabase()
    with patch.object(lab_db.db, 'supabase', f):
        r = _runner(f)
        r.cycle()
        opened = _open(f)
        assert len(opened) == 6 and {o['strategy_id'].split('_')[0] for o in opened} == {'GAPGO'}
        r.flatten('EOD')
    assert not _open(f)
    assert {t['exit_reason'] for t in f.tables['lab_trades']} == {'EOD'}
    assert all(not b.open for b in r.books.values())


def test_resume_reloads_open_positions_no_duplicate():
    f = FakeSupabase()
    with patch.object(lab_db.db, 'supabase', f):
        first = _runner(f)
        first.cycle()                                       # 6 OPEN rows, then "crash"
        clock = Clock('09:32:00')
        again = _runner(f, clock=clock)
        again.resume()
        assert len(again.books['GAPGO_s1_base_v1'].open) == 1
        assert again.books['GAPGO_s1_base_v1'].entries_today['AAA'] == 1
        again.tick()
    assert len(f.tables['lab_trades']) == 6                 # no duplicate entries for 09:30


def test_token_expired_flattens_and_returns():
    f = FakeSupabase()
    kite = FakeKite()
    with patch.object(lab_db.db, 'supabase', f):
        r = _runner(f, kite=kite)
        r.cycle()
        kite.fail = True
        assert r.run() == 'TOKEN_LOST'
    assert not _open(f)
    assert {t['exit_reason'] for t in f.tables['lab_trades']} == {'TOKEN_LOST'}
    assert any(x['reason'] == 'token' for x in f.tables['lab_refusals'])


def test_yields_while_advisor_running():
    f = FakeSupabase()
    kite = FakeKite()
    with patch.object(lab_db.db, 'supabase', f):
        _runner(f, kite=kite, should_yield=lambda: True).cycle()
    assert kite.hist_calls == 0 and not f.tables.get('lab_trades')


def test_finish_day_writes_one_row_per_variant_with_trades():
    f = FakeSupabase()
    with patch.object(lab_db.db, 'supabase', f):
        r = _runner(f)
        lab_db.register(lab.registry_rows())
        r.cycle()
        r.flatten('EOD')
        r.finish_day()
    rows = f.tables['lab_daily']
    assert len(rows) == 6 and all(x['cum_n'] == 1 and x['n'] == 1 for x in rows)
    assert all(x['sprt_state'] == 'running' for x in rows)


def test_run_full_day_ends_at_eod():
    f = FakeSupabase()
    with patch.object(lab_db.db, 'supabase', f):
        assert _runner(f, clock=Clock('09:29:50')).run() == 'EOD'
    assert f.tables['lab_trades'] and not _open(f)
    assert len(f.tables['lab_daily']) == 6


def test_scheduler_starts_single_lab_thread():
    import scheduler
    started = []

    class T:
        def __init__(self, target=None, daemon=None, name=None): self.name = name
        def start(self): started.append(self.name)
        def is_alive(self): return True

    now = IST.localize(datetime(2026, 11, 19, 9, 20))
    with patch.object(scheduler.config, 'LAB_ENABLED', True), \
            patch.object(scheduler.threading, 'Thread', T), \
            patch.object(scheduler.db, 'get_enc_token', return_value='tok'), \
            patch.object(scheduler, '_now_ist', return_value=now), \
            patch.object(scheduler, '_lab_thread', None):
        scheduler._maybe_run_lab()
        scheduler._maybe_run_lab()
    assert started == ['lab']


def test_yielded_cycle_retries_on_next_tick():
    f = FakeSupabase()
    busy = [True]
    clock = Clock('09:30:30')
    with patch.object(lab_db.db, 'supabase', f):
        r = _runner(f, clock=clock, should_yield=lambda: busy[0])
        r.tick()                                  # advisor busy: nothing fetched
        assert not f.tables.get('lab_trades')
        busy[0] = False
        clock.sleep(10)
        r.tick()                                  # same 09:30 boundary, now free
    assert len(_open(f)) == 6


def test_yield_mid_fetch_aborts_cycle():
    f = FakeSupabase()
    calls = {'n': 0}

    def should_yield():
        calls['n'] += 1
        return calls['n'] > 1                     # free at cycle start, busy by first symbol

    with patch.object(lab_db.db, 'supabase', f):
        assert _runner(f, should_yield=should_yield).cycle() is False
    assert not f.tables.get('lab_trades')
