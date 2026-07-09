"""M3 activation tests: opening-range stats, brain-side level-pack builder
and 09:30 in-play locker (idempotency, never-throw, lock-time gate)."""
import os
from unittest.mock import MagicMock, patch

import pytest

with patch.dict(os.environ, {
    'SUPABASE_URL': 'https://fake.supabase.co',
    'SUPABASE_SERVICE_KEY': 'fake-key',
}):
    with patch('supabase.create_client', return_value=MagicMock()):
        import database  # noqa

import config
import data_jobs
import inplay


def _c(o, h, l, cl, v, ts):
    return {'open': o, 'high': h, 'low': l, 'close': cl, 'volume': v,
            'timestamp': ts}


def _day(day, or_vol, n=10, base=100.0):
    """One day of 5-min candles; OR (first 3) volume sums to or_vol."""
    out = []
    for i in range(n):
        v = or_vol // 3 if i < 3 else 500
        out.append(_c(base, base + 1, base - 1, base + 0.2, v,
                      f'{day}T{9 + i // 12:02d}:{15 + (i % 12) * 5:02d}:00+0530'))
    return out


# --- opening_range_stats ---

def test_or_stats_computes_rvol_against_prior_days():
    candles = (_day('2026-07-06', 3000) + _day('2026-07-07', 3000)
               + _day('2026-07-08', 9000))
    s = inplay.opening_range_stats(candles)
    assert s['or_volume'] == 9000
    assert s['avg_or_volume'] == 3000
    assert s['or_rvol'] == 3.0
    assert s['gap_pct'] is not None


def test_or_stats_no_baseline_means_unranked():
    candles = _day('2026-07-08', 9000)  # today only
    s = inplay.opening_range_stats(candles)
    assert s['or_rvol'] is None
    assert s['avg_or_volume'] is None


def test_or_stats_empty_input():
    assert inplay.opening_range_stats([]) == {}


def test_or_high_low_from_first_three_candles():
    candles = _day('2026-07-07', 3000) + _day('2026-07-08', 3000)
    s = inplay.opening_range_stats(candles)
    assert s['or_high'] == 101.0 and s['or_low'] == 99.0


# --- maybe_build_level_pack ---

def _md_with_candles():
    md = MagicMock()
    md.get_candles.return_value = [
        _c(100 + i, 102 + i, 98 + i, 101 + i, 1000,
           f'2026-06-{(i % 28) + 1:02d}T10:00:00+0530')
        for i in range(40)
    ]
    return md


def test_level_pack_builds_once(monkeypatch):
    md = _md_with_candles()
    with patch('data_jobs.db.get_level_pack_map', return_value={}), \
         patch('data_jobs.db.upsert_level_pack') as up:
        n = data_jobs.maybe_build_level_pack(md, {'NSE:INFY': {}, 'NSE:TCS': {}})
    assert n == 2 and up.call_count == 2


def test_level_pack_skips_when_already_built():
    md = _md_with_candles()
    with patch('data_jobs.db.get_level_pack_map',
               return_value={'NSE:INFY': {'pdc': 100}}), \
         patch('data_jobs.db.upsert_level_pack') as up:
        n = data_jobs.maybe_build_level_pack(md, {'NSE:INFY': {}})
    assert n == 0
    up.assert_not_called()


def test_level_pack_builds_only_missing():
    # Self-heal: 1 of 2 already built (a partial prior run) — build only the
    # missing one instead of skipping the whole day (2026-07-09 trap).
    md = _md_with_candles()
    with patch('data_jobs.db.get_level_pack_map',
               return_value={'NSE:INFY': {'pdc': 100}}), \
         patch('data_jobs.db.upsert_level_pack') as up:
        n = data_jobs.maybe_build_level_pack(md, {'NSE:INFY': {}, 'NSE:TCS': {}})
    assert n == 1 and up.call_count == 1


def test_level_pack_aborts_on_token_expiry():
    # A dying token mid-build must abort the whole job (re-raise), not persist
    # a partial garbage set that then blocks the day.
    from kite_client import TokenExpiredError
    md = MagicMock()
    md.get_candles.side_effect = TokenExpiredError("expired")
    with patch('data_jobs.db.get_level_pack_map', return_value={}), \
         patch('data_jobs.db.upsert_level_pack'):
        with pytest.raises(TokenExpiredError):
            data_jobs.maybe_build_level_pack(md, {'NSE:INFY': {}, 'NSE:TCS': {}})


def test_level_pack_never_throws():
    md = MagicMock()
    md.get_candles.side_effect = RuntimeError("net down")
    with patch('data_jobs.db.get_level_pack_map', return_value={}), \
         patch('data_jobs.db.upsert_level_pack'):
        assert data_jobs.maybe_build_level_pack(md, {'NSE:INFY': {}}) == 0


# --- maybe_lock_inplay ---

def _md_with_or_candles(rvol_high=True):
    md = MagicMock()
    today_vol = 9000 if rvol_high else 100
    md.get_candles.return_value = (
        _day('2026-07-06', 3000) + _day('2026-07-07', 3000)
        + _day('2026-07-08', today_vol))
    return md


def test_inplay_locks_when_rvol_clears_bar(monkeypatch):
    monkeypatch.setattr(config, 'QA_MODE', True)  # bypass clock gate
    md = _md_with_or_candles(rvol_high=True)
    with patch('data_jobs.db.inplay_locked', return_value=False), \
         patch('data_jobs.db.lock_inplay_list', return_value=1) as lock:
        n = data_jobs.maybe_lock_inplay(md, {'NSE:INFY': {}})
    assert n == 1
    ranked = lock.call_args[0][1]
    assert ranked[0]['symbol'] == 'NSE:INFY' and ranked[0]['rank'] == 1


def test_inplay_retries_when_nothing_clears_bar(monkeypatch):
    monkeypatch.setattr(config, 'QA_MODE', True)
    md = _md_with_or_candles(rvol_high=False)
    with patch('data_jobs.db.inplay_locked', return_value=False), \
         patch('data_jobs.db.lock_inplay_list') as lock:
        n = data_jobs.maybe_lock_inplay(md, {'NSE:INFY': {}})
    assert n == 0
    lock.assert_not_called()   # unlocked → later cycle retries


def test_inplay_skips_when_already_locked(monkeypatch):
    monkeypatch.setattr(config, 'QA_MODE', True)
    with patch('data_jobs.db.inplay_locked', return_value=True), \
         patch('data_jobs.db.lock_inplay_list') as lock:
        n = data_jobs.maybe_lock_inplay(MagicMock(), {'NSE:INFY': {}})
    assert n == 0
    lock.assert_not_called()


def test_inplay_respects_lock_time_gate(monkeypatch):
    monkeypatch.setattr(config, 'QA_MODE', False)
    with patch('data_jobs._past_lock_time', return_value=False), \
         patch('data_jobs.db.inplay_locked') as locked_q:
        n = data_jobs.maybe_lock_inplay(MagicMock(), {'NSE:INFY': {}})
    assert n == 0
    locked_q.assert_not_called()  # gate short-circuits before any DB hit


def test_inplay_never_throws(monkeypatch):
    monkeypatch.setattr(config, 'QA_MODE', True)
    with patch('data_jobs.db.inplay_locked', side_effect=RuntimeError("boom")):
        assert data_jobs.maybe_lock_inplay(MagicMock(), {'NSE:INFY': {}}) == 0
