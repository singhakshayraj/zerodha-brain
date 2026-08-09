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

import time
from datetime import datetime

import pytz

import config
import database as db
import inplay
import level_pack
from kite_client import TokenExpiredError

IST = pytz.timezone('Asia/Kolkata')


def _today() -> str:
    return datetime.now(IST).strftime('%Y-%m-%d')


def maybe_build_level_pack(market_data, universe: dict) -> int:
    """Build today's missing level_pack rows. Returns rows written.

    Builds per-symbol only for symbols that don't already have a pack today,
    so a partial build self-heals on the next cycle instead of being blocked
    forever. Previously gated on level_pack_exists() — a mere "≥1 row exists"
    check — so a partial build (e.g. 2 of 46 under an expiring token on
    2026-07-09) permanently stranded the day at that handful of packs and fed
    garbage PDCs to breadth/level consumers."""
    try:
        today = _today()
        existing = set(db.get_level_pack_map(today).keys())
        missing = [key for key in universe if key not in existing]
        if not missing:
            return 0
        print(f"[data_jobs] Building level pack for {today}… "
              f"({len(missing)} missing, {len(existing)} already built)")
        written = 0
        for key in missing:
            try:
                candles = market_data.get_candles(key, '60minute', days=60)
                daily = level_pack.daily_ohlc(candles)
                if not daily:
                    continue
                db.upsert_level_pack(level_pack.build(key, today, daily))
                written += 1
            except TokenExpiredError:
                # Building the rest under a dying token is exactly how the day
                # got stuck with a few garbage packs. Stop now — build-missing
                # is idempotent, so a later cycle with a fresh token completes
                # it — and let the token failure surface upstream.
                print("[data_jobs] token expired mid-build — aborting "
                      f"(built {written} this pass, will resume next cycle)")
                raise
            except Exception as e:
                print(f"[data_jobs] level pack {key} failed: {e}")
        print(f"[data_jobs] Level pack: +{written} "
              f"({len(existing) + written}/{len(universe)} symbols)")
        return written
    except TokenExpiredError:
        raise
    except Exception as e:
        print(f"[data_jobs] level pack job failed (non-fatal): {e}")
        return 0


def _past_lock_time() -> bool:
    if config.QA_MODE:
        return True  # off-hours rehearsals: lock on the first cycle
    now = datetime.now(IST)
    return (now.hour * 60 + now.minute) >= (9 * 60 + 30)


def build_weekly_profiles(market_data, asof: str = None,
                          lookback_days: int = 90) -> int:
    """Behavioural fingerprint per universe symbol (ENGINEERING_SPEC M3):
    trendiness, gap-follow rate, range profile → stock_profile table.
    Extracted from scripts/build_profiles.py so the scheduler can run it
    weekly instead of depending on a Mac cron that never got installed
    (table sat at 0 rows for weeks). Read-only + upserts; returns rows
    written. Per-symbol failures skip that symbol."""
    import level_pack
    import stock_profile
    asof = asof or datetime.now(IST).strftime('%Y-%m-%d')
    tokens = dict(config.NIFTY50_INSTRUMENT_TOKENS)
    tokens.update(getattr(config, 'NIFTY_NEXT50_INSTRUMENT_TOKENS', {}))

    dailies = {}
    for sym, token in tokens.items():
        try:
            market_data._instrument_cache[sym] = token
            candles = market_data.get_candles(sym, '60minute',
                                              days=lookback_days)
            dailies[sym] = level_pack.daily_ohlc(candles)
            # Pace like the advisor's universe scan — ~100 historical fetches
            # on the shared Kite session must never arrive as a burst.
            time.sleep(config.ADVISOR_UNIVERSE_SCAN_DELAY_MS / 1000.0)
        except Exception as e:
            print(f"[data_jobs.profiles] {sym} fetch failed: {e}")
            dailies[sym] = []

    trends, gaps = [], []
    for sym, daily in dailies.items():
        if len(daily) >= stock_profile.MIN_SAMPLES:
            t = stock_profile.efficiency_ratio([d['close'] for d in daily])
            g = stock_profile.gap_follow_rate(daily)['rate']
            if t is not None:
                trends.append(t)
            if g is not None:
                gaps.append(g)
    universe_avg = {
        'trendiness': round(sum(trends) / len(trends), 4) if trends else None,
        'gap_follow_rate': round(sum(gaps) / len(gaps), 4) if gaps else None,
    }

    ok = 0
    for sym, daily in dailies.items():
        try:
            row = stock_profile.build(sym, asof, daily, lookback_days,
                                      universe_avg=universe_avg)
            db.upsert_stock_profile(row)
            ok += 1
        except Exception as e:
            print(f"[data_jobs.profiles] {sym} failed: {e}")
    print(f"[data_jobs.profiles] built {ok}/{len(dailies)} profiles "
          f"asof {asof}")
    return ok


def _market_hours_now() -> bool:
    now = datetime.now(IST)
    if now.weekday() > 4:
        return False
    m = now.hour * 60 + now.minute
    return (9 * 60 + 15) <= m <= (15 * 60 + 35)


def maybe_weekly_profiles(market_data) -> int:
    """Run the profile builder once per ISO week (durable marker in
    app_config 'profiles_week'). Refuses to run during market hours even
    paced — ~100 historical fetches belong in the post-close/overnight
    window, never next to the live trading loop. Non-fatal by construction."""
    try:
        if _market_hours_now():
            return 0
        week = datetime.now(IST).strftime('%G-W%V')
        if (db.get_config('profiles_week') or '') == week:
            return 0
        n = build_weekly_profiles(market_data)
        if n:
            db.write_config('profiles_week', week)
        return n
    except Exception as e:
        print(f"[data_jobs.profiles] weekly job failed (non-fatal): {e}")
        return 0


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
            # Diagnostics so a zero-lock day is explainable from the log
            # alone (bug vs genuinely quiet tape) — 2026-07-13 burned an
            # audit on exactly this ambiguity.
            rvols = sorted((c['or_rvol'] for c in candidates
                            if c.get('or_rvol') is not None), reverse=True)
            print(f"[data_jobs] No candidates cleared the RVOL bar "
                  f"(threshold {config.RVOL_THRESHOLD}; {len(candidates)} "
                  f"scanned, {len(rvols)} with known RVOL, "
                  f"top3 {[round(r, 2) for r in rvols[:3]]})")
            # Capture-first fallback (KNOWN_ISSUES P5, data-collection only):
            # a quiet tape is itself a sample. Lock the best-available names
            # with their true below-bar RVOLs — or_rvol is stored per row, so
            # anything downstream that means "cleared the bar" re-filters to
            # >= RVOL_THRESHOLD. Plain mode keeps the strict gate + retry.
            if config.data_collection_active():
                ranked = inplay.rank(candidates, min_rvol=0.0)[
                    :config.INPLAY_FALLBACK_TOP_N]
                if ranked:
                    print(f"[data_jobs] capture-first fallback: locking "
                          f"top {len(ranked)} below-bar names")
            if not ranked:
                return 0            # unlocked -> a later cycle retries
        return db.lock_inplay_list(today, ranked)
    except Exception as e:
        print(f"[data_jobs] inplay job failed (non-fatal): {e}")
        return 0


# --- [C5] post-close candle backfill ----------------------------------------

def _token_for(symbol: str):
    """Pinned instrument token for an NSE symbol, or None. Post-close there is
    no live universe, so MarketData's instrument cache is empty — the pinned
    maps are the only source."""
    key = f'NSE:{symbol}'
    return (config.NIFTY500_INSTRUMENT_TOKENS.get(key)
            or config.NIFTY50_INSTRUMENT_TOKENS.get(key))


def archive_traded_day_candles(market_data, session_id: str, run_date: str,
                               symbols: list = None) -> int:
    """Re-archive the FULL day's 5-minute bars for every symbol traded today.

    Fixes [C5]. The in-cycle archive writes only the trailing 3 bars, and only
    for symbols analyzed that cycle — so a position that closes between cycles
    never gets its final bars written. [P-30] measured the cost: 10 of 118
    clean-exit trades exit past the last archived bar, which makes their exit
    unorderable by the candle replay.

    ⚠️ Runs POST-CLOSE, never in the close path. Adding an archive call to the
    exit path is the documented `archive_candles` latency regression (~7s/cycle)
    and a slow exit path is measured to fill stops at −2.78R instead of ≈−1R.
    Here latency is irrelevant.

    Idempotent: candles upsert on (symbol, interval, ts), so re-running only
    fills gaps. Never raises — a data job must not take anything down.

    Returns the number of bars written.
    """
    if symbols is None:
        symbols = db.traded_symbols_on(run_date)
    if not symbols:
        print('[data_jobs.candle_backfill] no symbols traded — nothing to do')
        return 0

    total, done, skipped = 0, 0, []
    for sym in symbols:
        token = _token_for(sym)
        if not token:
            skipped.append(sym)
            continue
        try:
            # Seed the instrument cache and use the public path, so caching,
            # error handling and TokenExpiredError propagation stay identical
            # to every other candle read.
            market_data._instrument_cache[sym] = token
            candles = market_data.get_candles(sym, '5minute', 3) or []
            todays = [c for c in candles
                      if str(c.get('timestamp', ''))[:10] == run_date]
            if not todays:
                continue
            # tail=len(todays): the whole day, not the usual trailing 3 — the
            # point is to fill gaps the trailing writes left behind.
            rows = db.candle_rows(session_id, sym, 'NSE', todays,
                                  tail=len(todays))
            total += db.upsert_candles(rows)
            done += 1
        except TokenExpiredError:
            print('[data_jobs.candle_backfill] token expired — stopping')
            break
        except Exception as e:
            print(f"[data_jobs.candle_backfill] {sym} failed: {e}")

    if skipped:
        print(f"[data_jobs.candle_backfill] no pinned token for "
              f"{len(skipped)}: {', '.join(skipped[:8])}")
    print(f"[data_jobs.candle_backfill] {run_date}: {total} bars over "
          f"{done}/{len(symbols)} symbols")
    return total
