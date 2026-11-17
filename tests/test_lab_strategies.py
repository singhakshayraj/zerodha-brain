from datetime import datetime, timedelta

import pytest
import pytz

from lab_strategies import (Ctx, VARIANTS, completed_bars, prior_close, registry_rows,
                            resample)

IST = pytz.timezone('Asia/Kolkata')
TODAY = '2026-11-19'


def _bar(ts, close=100.0, o=None, h=None, l=None, v=1000):
    o = close if o is None else o
    return {'timestamp': f'{ts}:00+0530', 'open': o, 'high': max(o, close) if h is None else h,
            'low': min(o, close) if l is None else l, 'close': close, 'volume': v}


def _ist(s):
    return IST.localize(datetime.strptime(s, '%Y-%m-%d %H:%M:%S'))


def _variant(vid):
    return next(v for v in VARIANTS if v.id == vid)


def _ctx(bars, ltp, now, rank_adv=10, rs_rank=None, symbol='X'):
    return Ctx(now_ist=_ist(f'{TODAY} {now}:30'), symbol=symbol, bars=bars, ltp=ltp,
               nifty_bars=[], rank_adv=rank_adv, rs_rank=rs_rank or {})


def _ctx_gap(prior, open_, first15_low, now, close_0925=None):
    bars = [_bar('2026-11-18T15:25', close=prior)]
    for i, t in enumerate(('09:15', '09:20', '09:25')):
        c = open_ if (i < 2 or close_0925 is None) else close_0925
        bars.append(_bar(f'{TODAY}T{t}', close=c, o=open_, h=max(open_, c) + 0.5, l=first15_low))
    return _ctx(bars, ltp=bars[-1]['close'], now=now)


def _ctx_vwap(dev_atr, rank_adv):
    start = datetime(2026, 11, 19, 9, 15)
    bars = [_bar((start + timedelta(minutes=5 * i)).strftime('%Y-%m-%dT%H:%M'),
                 close=100.0, h=101.0, l=99.0) for i in range(20)]
    return _ctx(bars, ltp=100.0 + dev_atr * 2.0, now='11:00', rank_adv=rank_adv)


def _ctx_any(now):
    return _ctx_gap(prior=100, open_=103, first15_low=102, now=now)


def test_registry_has_54_unique_ids_and_stable_hash():
    rows = registry_rows()
    assert len(rows) == 54 == len({r['id'] for r in rows})
    assert registry_rows()[0]['spec_hash'] == rows[0]['spec_hash']
    assert len({r['spec_hash'] for r in rows}) == 54


def test_completed_bars_drops_in_progress_bar():
    bars = [_bar(f'{TODAY}T09:50'), _bar(f'{TODAY}T09:55'), _bar(f'{TODAY}T10:00')]
    got = completed_bars(bars, _ist(f'{TODAY} 10:02:30'))
    assert [b['timestamp'][11:16] for b in got] == ['09:50', '09:55']


def test_prior_close_skips_weekend():
    bars = [_bar('2026-11-20T15:25', close=500), _bar('2026-11-23T09:15', close=510)]
    assert prior_close(bars) == 500


def test_resample_15min_buckets_from_0915():
    bars = [_bar(f'{TODAY}T09:{m:02d}', close=100 + i, v=10) for i, m in enumerate((15, 20, 25, 30))]
    out = resample(bars, 15)
    assert len(out) == 2 and out[0]['close'] == 102 and out[0]['volume'] == 30
    assert out[1]['timestamp'].startswith(f'{TODAY}T09:30')


def test_gapgo_long_only_when_gap_holds():
    v = _variant('GAPGO_s1_base_v1')
    ctx = _ctx_gap(prior=100, open_=102, first15_low=101, now='09:30')
    assert v.signal(ctx) == {'side': 'LONG', 'stop': pytest.approx(ctx.ltp * 0.99), 'target': None}
    assert v.signal(_ctx_gap(prior=100, open_=102, first15_low=99.5, now='09:30')) is None
    assert v.signal(_ctx_gap(prior=100, open_=102, first15_low=101, now='09:35')) is None


def test_gapfade_short_toward_prior_close():
    v = _variant('GAPFADE_s1_base_v1')
    sig = v.signal(_ctx_gap(prior=100, open_=102, first15_low=101, now='09:30', close_0925=101.5))
    assert sig['side'] == 'SHORT' and sig['target'] == 100 and sig['stop'] > 101.5


def test_no_entries_after_1430():
    assert all(v.signal(_ctx_any(now='14:35')) is None for v in VARIANTS)


def test_vwaprev_only_top100():
    v = _variant('VWAPREV_s1_base_v1')
    assert v.signal(_ctx_vwap(dev_atr=2.5, rank_adv=150)) is None
    sig = v.signal(_ctx_vwap(dev_atr=2.5, rank_adv=50))
    assert sig['side'] == 'SHORT' and sig['target'] == pytest.approx(100.0)
    assert v.signal(_ctx_vwap(dev_atr=1.0, rank_adv=50)) is None


def test_rshold_top_k_long_bottom_k_short_at_1015():
    order = [f'S{i}' for i in range(20)]
    v = _variant('RSHOLD_s2_base_v1')          # K=5
    long_ = v.signal(_ctx(_ctx_any('09:30').bars, 100.0, '10:15', rs_rank={'order': order}, symbol='S4'))
    short = v.signal(_ctx(_ctx_any('09:30').bars, 100.0, '10:15', rs_rank={'order': order}, symbol='S15'))
    assert long_['side'] == 'LONG' and long_['stop'] == pytest.approx(98.0)
    assert short['side'] == 'SHORT'
    assert v.signal(_ctx(_ctx_any('09:30').bars, 100.0, '10:15', rs_rank={'order': order}, symbol='S5')) is None
    assert v.signal(_ctx(_ctx_any('09:30').bars, 100.0, '10:20', rs_rank={'order': order}, symbol='S4')) is None


def test_every_variant_tolerates_thin_bars_midmorning():
    for v in VARIANTS:
        sig = v.signal(_ctx_any(now='10:00'))
        assert sig is None or sig['side'] in ('LONG', 'SHORT')
