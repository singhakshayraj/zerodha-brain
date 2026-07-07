"""Brain-side M3 data jobs — level pack at session start, in-play lock at
09:30 (ENGINEERING_SPEC M3, amended).

Spec §1 put these on a Mac cron at 07:00 IST, but retail enctoken auth makes
that impossible: the old token dies ~06:00 and the fresh one is pasted just
before 09:15. So the jobs run inside the brain, where a valid token is
guaranteed — level pack right after initialize (data is prior-day, so
building at 09:1x loses nothing), in-play locked at the first cycle past
09:30 (spec's exact lock time).

Both are idempotent (DB-existence guards, fail-closed), non-gating (the
universe is unchanged — rows are collected for M4/M5), and never throw:
a data-job failure must never take down a trading cycle.
"""

from datetime import datetime

import pytz

import config
import database as db
import inplay
import level_pack

IST = pytz.timezone('Asia/Kolkata')


def _today() -> str:
    return datetime.now(IST).strftime('%Y-%m-%d')


def maybe_build_level_pack(market_data, universe: dict) -> int:
    """Build today's level_pack rows once per day. Returns rows written."""
    try:
        today = _today()
        if db.level_pack_exists(today):
            return 0
        print(f"[data_jobs] Building level pack for {today}…")
        written = 0
        for key in universe:
            try:
                candles = market_data.get_candles(key, '60minute', days=60)
                daily = level_pack.daily_ohlc(candles)
                if not daily:
                    continue
                db.upsert_level_pack(level_pack.build(key, today, daily))
                written += 1
            except Exception as e:
                print(f"[data_jobs] level pack {key} failed: {e}")
        print(f"[data_jobs] Level pack done: {written}/{len(universe)} symbols")
        return written
    except Exception as e:
        print(f"[data_jobs] level pack job failed (non-fatal): {e}")
        return 0


def _past_lock_time() -> bool:
    if config.QA_MODE:
        return True  # off-hours rehearsals: lock on the first cycle
    now = datetime.now(IST)
    return (now.hour * 60 + now.minute) >= (9 * 60 + 30)


def maybe_lock_inplay(market_data, universe: dict) -> int:
    """Lock today's in-play list once, at/after 09:30. Returns rows locked.
    Non-gating during the paper run — the list is recorded, not enforced."""
    try:
        if not _past_lock_time():
            return 0
        today = _today()
        if db.inplay_locked(today):
            return 0
        print(f"[data_jobs] Locking in-play list for {today}…")
        candidates = []
        for key in universe:
            try:
                candles = market_data.get_candles(key, '5minute', days=5)
                stats = inplay.opening_range_stats(candles)
                if not stats:
                    continue
                stats['symbol'] = key
                candidates.append(stats)
            except Exception as e:
                print(f"[data_jobs] inplay {key} failed: {e}")
        ranked = inplay.rank(candidates)
        if not ranked:
            # Lock an explicit empty marker? No — leaving it unlocked lets a
            # later cycle retry (e.g. candles were thin at 09:30 sharp).
            print("[data_jobs] No candidates cleared the RVOL bar — will retry next cycle")
            return 0
        return db.lock_inplay_list(today, ranked)
    except Exception as e:
        print(f"[data_jobs] inplay job failed (non-fatal): {e}")
        return 0
