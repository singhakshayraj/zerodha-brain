import signal
import sys
import threading
import time
import uuid
from datetime import datetime

import pytz

import config
import database as db
import telegram
import token_refresher
from brain import TradingBrain
from kite_client import KiteClient, TokenExpiredError
from risk_manager import RiskManager

IST = pytz.timezone('Asia/Kolkata')
risk_manager = RiskManager()

_brain_lock = threading.Lock()
_is_trading = False

# Unique per-process id. Written to app_config at startup: newest process
# owns the control plane. During a Railway redeploy the old and new
# containers overlap for a while — without this, BOTH resume the same
# RUNNING session and double-trade it.
INSTANCE_ID = uuid.uuid4().hex

# (session_id, sha) pairs this process has already reported as REQ-072
# incidents — a failing resume + autopilot retry loop must not re-report
# the same incident every pass (2026-07-08: one incident spammed for an hour).
_reported_deploy_incidents = set()

# Latched so a stale token doesn't re-write the durable incident + feed line
# every ~30s autopilot retry (2026-07-08 spam lesson). Cleared once a live
# token starts a session.
_stale_token_reported = False


def _token_is_live(token: str) -> bool:
    """Cheap authenticated probe (/user/profile) so we don't create a session
    on a dead token. db.get_enc_token() returns the stored string, which is
    still non-empty once yesterday's token has expired — so without this,
    autopilot self-starts at 09:30 on a stale token, fails init, and (because
    has_session_today() then reports True) never retries, burning the whole
    morning (2026-07-09 lost 09:15-12:44). Returns True in QA and on
    inconclusive/transient errors — only a hard TokenExpiredError blocks the
    start, so a network blip can't strand the brain."""
    if config.QA_MODE:
        return True
    try:
        KiteClient(token).get_profile()
        return True
    except TokenExpiredError:
        return False
    except Exception as e:
        print(f"[SCHEDULER] token probe inconclusive ({e}) — allowing start")
        return True


# Portfolio advisor concurrency guard only — dedup itself is DB-backed
# (db.has_official_advisor_run / get_last_advisor_run_time), not an in-memory
# flag. 2026-07-14: the old in-memory `_advisor_date` was set BEFORE the run
# executed, so a transient failure (e.g. holdings fetch hiccup) still
# consumed the day's one attempt with zero rows written and no retry until a
# lucky redeploy reset the flag. DB-backed dedup fixes both that and the
# redeploy-fragility in one move.
_advisor_running = False
_advisor_started_at = None   # when the current advisor thread began (staleness)
_preflight_date = None


def _parse_hhmm(hhmm: str, fallback: tuple) -> tuple:
    """'09:45' -> (9, 45); malformed config falls back rather than crashing
    the scheduler loop."""
    try:
        h, m = hhmm.split(':')
        return int(h), int(m)
    except Exception:
        return fallback


def _maybe_token_preflight() -> None:
    """Pre-flight token health check, once per market day at/after
    ADVISOR_PREFLIGHT_IST (09:16). A dead or missing enc_token this early is
    still fixable before the advisor run needs it — push a high-priority
    Telegram alert instead of silently skipping the day. Never raises."""
    global _preflight_date
    if config.QA_MODE:
        return
    try:
        now = datetime.now(IST)
        today = now.date().isoformat()
        if _preflight_date == today or now.weekday() > 4 \
                or today in config.NSE_HOLIDAYS:
            return
        ph, pm = _parse_hhmm(config.ADVISOR_PREFLIGHT_IST, (9, 16))
        if (now.hour, now.minute) < (ph, pm):
            return
        _preflight_date = today
        token = db.get_enc_token()
        if token and _token_is_live(token):
            print("[SCHEDULER] token preflight OK")
            return
        run_at = config.ADVISOR_RUN_AFTER_IST
        if config.ADVISOR_TELEGRAM_BOT_TOKEN and config.ADVISOR_TELEGRAM_CHAT_ID:
            print("[SCHEDULER] token preflight FAILED — alerting via Telegram")
            telegram.send_message(
                config.ADVISOR_TELEGRAM_BOT_TOKEN,
                config.ADVISOR_TELEGRAM_CHAT_ID,
                f"⚠️ Kite session expired/missing. Paste a fresh enc_token "
                f"before the {run_at} IST advisor run!")
    except Exception as e:
        print(f"[SCHEDULER] token preflight errored (non-fatal): {e}")


def _now_ist() -> datetime:
    """Isolates 'current time' behind a plain function so tests can patch
    just this, leaving the real datetime class (which _parse_iso_dt below
    needs for fromisoformat/isinstance) untouched."""
    return datetime.now(IST)


def _parse_iso_dt(value) -> "datetime | None":
    """created_at from Supabase comes back as an ISO string; tolerate a
    datetime too. Returns None on anything unparseable rather than raising —
    a bad timestamp should degrade to 'run again', never crash the loop."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else IST.localize(value)
    try:
        dt = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return dt.astimezone(IST) if dt.tzinfo else IST.localize(dt)
    except Exception:
        return None


def _grade_advice_catchup(token: str) -> None:
    """Grade every currently-due, unevaluated advice row on a background thread.

    Runs once at session start, where a token is proven live — the once/day
    official-advisor branch (`_maybe_run_advisor`) missed maturity days during
    token gaps and starved the backlog to 38 matured-but-ungraded rows by
    2026-08. A single `run_backtest_pass` re-queries and drains the whole
    backlog, so one reliable call per session keeps the advisor track record
    honest. Read-only (daily candles), advisory — never touches orders."""
    def _run():
        try:
            import advisor_backtest
            from market_data import MarketData
            md = MarketData(KiteClient(token))
            n = advisor_backtest.run_backtest_pass(md)
            print(f"[SCHEDULER] advice grading catch-up: graded {n} due rows")
        except Exception as e:
            print(f"[SCHEDULER] advice grading catch-up failed (non-fatal): {e}")

    threading.Thread(target=_run, daemon=True).start()


def _label_decisions_catchup() -> None:
    """Counterfactual-label recent unlabeled decisions (Track C) on a background
    thread. Reads only brain_decisions + the candle archive — NO token needed.
    label_decisions.py was manual-only and nobody ran it after 2026-07-23, so
    the decision→outcome dataset (the feed for the P-21 edge study + any
    data-derived signal) starved for weeks. One catch-up per session over a
    trailing window keeps it fresh; label_decisions_for_date skips rows already
    labeled, so re-runs are cheap and idempotent."""
    def _run():
        try:
            import decision_outcomes
            from datetime import date, timedelta
            total = 0
            # Yesterday back 10 days — today's candles aren't archived until
            # post-close, so today isn't labelable at session start anyway.
            for i in range(1, 11):
                d = (date.today() - timedelta(days=i)).isoformat()
                total += decision_outcomes.label_decisions_for_date(d)
            print(f"[SCHEDULER] decision-labeling catch-up: labeled {total} rows")
        except Exception as e:
            print(f"[SCHEDULER] decision-labeling catch-up failed (non-fatal): {e}")

    threading.Thread(target=_run, daemon=True).start()


def _maybe_serve_lookup() -> None:
    """On-demand single-stock lookup. The dashboard queues a symbol in
    app_config 'advisor_lookup_request'; this scores it NOW with the live token
    (pull-mode) and writes the result to stock_universe, so /advisor lookups
    need not wait for the once-daily scan. The request is CLAIMED (cleared)
    before the worker starts, so a second idle pass never double-processes."""
    try:
        raw = db.get_config('advisor_lookup_request')
    except Exception:
        return
    if not raw:
        return
    db.write_config('advisor_lookup_request', '')      # claim it

    def _run():
        import json
        try:
            sym = ((json.loads(raw) or {}).get('symbol') or '').strip().upper()
            if not sym:
                return
            token = db.get_enc_token()
            if not token:
                db.write_config('advisor_lookup_error', f'{sym}: no token')
                return
            import config
            import portfolio_advisor
            from advisor_scoring import NIFTY50_INDEX_TOKEN
            from kite_client import KiteClient
            from market_data import MarketData
            itok = config.NIFTY500_INSTRUMENT_TOKENS.get(f'NSE:{sym}')
            if not itok:
                db.write_config('advisor_lookup_error', f'{sym}: not in universe')
                return
            md = MarketData(KiteClient(token))
            nifty_closes = []
            try:
                md._instrument_cache['NSE:NIFTY 50'] = NIFTY50_INDEX_TOKEN
                nb = md.get_candles('NSE:NIFTY 50', 'day', 400) or []
                nifty_closes = [float(c['close']) for c in nb
                                if c.get('close') is not None]
            except Exception:
                pass
            row = portfolio_advisor.score_one_symbol(md, sym, itok,
                                                     nifty_closes=nifty_closes)
            db.write_config('advisor_lookup_error',
                            '' if row else f'{sym}: insufficient data')
            print(f"[advisor.lookup] served {sym}: {'ok' if row else 'no data'}")
        except Exception as e:
            print(f"[advisor.lookup] failed (non-fatal): {e}")

    threading.Thread(target=_run, daemon=True).start()


_catchup_date = None


_amfi_nav_date = None


def _maybe_ingest_amfi_nav() -> None:
    """Once/day: pull AMFI's public NAV file and upsert. No token (public
    data). Non-fatal and daemon-threaded; the amfi_nav table is operator-applied
    DDL, so until it exists this simply no-ops. The date guard is set before
    the thread so a failed fetch waits until tomorrow rather than re-spawning."""
    global _amfi_nav_date
    today = datetime.now(IST).date().isoformat()
    if _amfi_nav_date == today:
        return
    _amfi_nav_date = today

    def _run():
        try:
            import amfi_nav
            n = db.upsert_amfi_nav(amfi_nav.fetch_and_parse())
            print(f"[amfi_nav] ingested {n} NAV rows for {today}")
        except Exception as e:
            print(f"[amfi_nav] ingest failed (non-fatal): {e}")

    threading.Thread(target=_run, daemon=True).start()


_bars_thread = None
_bars_attempt = None


def _maybe_sync_daily_bars() -> bool:
    """Daily-bar store FIRST whenever a live token is present (user rule
    2026-11-11). One attempt per (target day, token) -- a fresh paste
    retries. Returns True while the sync runs so the advisor waits: both
    hit Kite's historical endpoint and must not race its rate limit."""
    global _bars_thread, _bars_attempt
    if _bars_thread is not None and _bars_thread.is_alive():
        return True
    import data_jobs
    target = data_jobs.last_completed_trading_day(datetime.now(IST))
    token = db.get_enc_token()
    if not token or _bars_attempt == (target, token):
        return False
    _bars_attempt = (target, token)

    def _run():
        try:
            from market_data import MarketData
            data_jobs.sync_daily_bars(MarketData(KiteClient(token)))
        except Exception as e:
            print(f"[daily_bars] sync failed (non-fatal): {e}")

    _bars_thread = threading.Thread(target=_run, daemon=True, name='daily_bars')
    _bars_thread.start()
    return True


_preview_done = {}     # acct -> (date, token) already attempted


def _maybe_run_account_previews() -> None:
    """P-45 provisional: once per trading day per preview account, after the
    primary advisor has stored today's official batch, advise that account's
    holdings with ITS token into app_config 'advisor_<acct>_latest'. Never
    touches portfolio_advice. A fresh token paste re-arms the attempt."""
    import account_preview
    now = _now_ist()
    today = now.date().isoformat()
    if config.QA_MODE or now.weekday() > 4 or today in config.NSE_HOLIDAYS:
        return
    rh, rm = _parse_hhmm(config.ADVISOR_RUN_AFTER_IST, (9, 45))
    if (now.hour, now.minute) < (rh, rm):
        return
    for acct in account_preview.PREVIEW_ACCOUNTS:
        token = db.get_config(account_preview.token_key(acct))
        if not token or _preview_done.get(acct) == (today, token):
            continue
        _preview_done[acct] = (today, token)
        if not _token_is_live(token):
            print(f"[preview:{acct}] token not live — skipped today")
            continue

        def _run(acct=acct, token=token):
            try:
                from market_data import MarketData
                account_preview.run_preview(acct, MarketData(KiteClient(token)))
            except Exception as e:
                print(f"[preview:{acct}] failed (non-fatal): {e}")

        threading.Thread(target=_run, daemon=True, name=f'preview_{acct}').start()


def _maybe_run_catchups() -> None:
    """Once/day: label recent decisions and grade matured advice. Hoisted from
    the removed session-start path (Phase C.2). Both spawn a daemon thread
    internally, so the date guard stops the idle loop re-spawning them."""
    global _catchup_date
    today = datetime.now(IST).date().isoformat()
    if _catchup_date == today:
        return
    _catchup_date = today
    _label_decisions_catchup()
    if config.ADVISOR_BACKTEST_ENABLED:
        token = db.get_enc_token()
        if token:
            _grade_advice_catchup(token)


def _maybe_run_advisor() -> None:
    """Portfolio advisory (ADVISORY ONLY — no orders).

    The FIRST run of the day, once past ADVISOR_RUN_AFTER_IST (09:45 — past
    the opening-bell noise window) with a live token, is the "official" run:
    full analysis + Nifty-500 rotation scan + Telegram digest +
    backtest-eligible. Every ADVISOR_REFRESH_INTERVAL_SECONDS after that
    (2026-07-14, until market close), a lightweight intraday re-score fires
    instead — fresh price/verdict stored as a new snapshot row, no rotation
    rescan/digest — so the UI always reflects current market data and the
    dataset accrues an intraday time series per holding.

    A manual trigger (app_config 'advisor_run_now'=='true') forces an
    official-style run immediately regardless of gates — on-demand, from the
    dashboard or directly in app_config; cleared immediately so it fires once.
    Runs on a daemon thread so the scheduler loop never waits on candle
    fetches. Skipped in QA (synthetic market has no real holdings) and on
    non-trading days."""
    global _advisor_running, _advisor_started_at
    if config.QA_MODE:
        return
    if _advisor_running:
        # Staleness self-heal (P-17): a wedged advisor thread must not strand
        # the whole day. On 2026-07-28 the run fired ~5h late; if a thread hangs
        # (e.g. a slow/hung Kite call), reset the flag after 10min so the gate
        # can retry instead of silently blocking every tick.
        stale = (_advisor_started_at and
                 (datetime.now(IST) - _advisor_started_at).total_seconds() > 600)
        if not stale:
            return
        print("[SCHEDULER] advisor thread stale >10min — resetting the running "
              "flag so today's run can retry")
        _advisor_running = False
    now = _now_ist()
    today = now.date().isoformat()

    # Trading days only, INCLUDING for a manual trigger. Tempting to let a
    # forced run through on a weekend (scores go stale over one), but a forced
    # run writes an official advice batch stamped with that date, so a
    # Saturday-dated batch would have its grading horizon measured from a
    # Saturday. tests/test_advisor_trigger.py pins this deliberately:
    # the trigger bypasses the TIME window, never the trading-day gate.
    if now.weekday() > 4 or today in config.NSE_HOLIDAYS:
        return

    forced = (db.get_config('advisor_run_now') or '').strip().lower() == 'true'
    if forced:
        is_official_run = True
    else:
        have_official = db.has_official_advisor_run(today)
        if not have_official:
            rh, rm = _parse_hhmm(config.ADVISOR_RUN_AFTER_IST, (9, 45))
            if (now.hour, now.minute) < (rh, rm):
                return
            is_official_run = True
        else:
            ch, cm = config.MARKET_CLOSE_HOUR, config.MARKET_CLOSE_MINUTE
            if (now.hour, now.minute) >= (ch, cm):
                return
            last_run_at = _parse_iso_dt(db.get_last_advisor_run_time(today))
            if last_run_at and (now - last_run_at).total_seconds() \
                    < config.ADVISOR_REFRESH_INTERVAL_SECONDS:
                return
            is_official_run = False

    token = db.get_enc_token()
    if not token or not _token_is_live(token):
        # The silent killer — log it so a stalled advisor is diagnosable from
        # logs alone (P-17); the time-based skips above are normal + frequent
        # and deliberately stay quiet.
        # NOTE: a forced request is deliberately NOT consumed here. Consuming
        # on read meant a user pressing "re-run" with a stale token had the
        # request silently eaten — the exact case the button exists for. Left
        # set, it simply fires the moment a live token appears.
        print(f"[SCHEDULER] advisor gate ({'official' if is_official_run else 'lite'})"
              f" → skip: no live token"
              f"{' (forced request kept pending)' if forced else ''}")
        return
    _advisor_running = True
    _advisor_started_at = datetime.now(IST)
    print(f"[SCHEDULER] advisor gate → running "
          f"({'forced' if forced else 'official' if is_official_run else 'lite'})")

    def _run():
        global _advisor_running
        try:
            import portfolio_advisor
            from market_data import MarketData
            md = MarketData(KiteClient(token))
            if is_official_run:
                n = portfolio_advisor.run_advisor(md)
                print(f"[SCHEDULER] advisor (official) done: {n} holdings analyzed")
                if config.ADVISOR_BACKTEST_ENABLED:
                    try:
                        import advisor_backtest
                        advisor_backtest.run_backtest_pass(md)
                    except Exception as e:
                        print(f"[SCHEDULER] advisor backtest failed "
                              f"(non-fatal): {e}")
                # Paper-portfolio (P-14): act on today's official verdicts across
                # both virtual books. Advisory-only; never touches orders.
                if config.ADVISOR_PAPER_ENABLED:
                    try:
                        import advisor_paper
                        advisor_paper.run_paper_portfolio(md)
                    except Exception as e:
                        print(f"[SCHEDULER] advisor paper-portfolio failed "
                              f"(non-fatal): {e}")
                # Weekly stock profiles (M3): no-ops during market hours (the
                # builder refuses to run next to the live loop); actually
                # fires from the post-close slot below or an off-hours run.
                try:
                    import data_jobs
                    data_jobs.maybe_weekly_profiles(md)
                except Exception as e:
                    print(f"[SCHEDULER] weekly profiles failed (non-fatal): {e}")
            else:
                n = portfolio_advisor.run_advisor_lite(md)
                print(f"[SCHEDULER] advisor (intraday refresh) done: {n} holdings analyzed")

            # [P-25] Real-money accountability. Runs after EITHER branch,
            # because the intraday refresh is what gives the holdings series
            # its ~8-minute resolution — a sale shows up as the holding
            # shrinking on the very next pass. Bounded lookback; the advice
            # rows it diffs were just written above.
            try:
                import user_executions
                user_executions.run_user_executions()
            except Exception as e:
                print(f"[SCHEDULER] user-execution sync failed (non-fatal): {e}")
        except Exception as e:
            print(f"[SCHEDULER] advisor failed (non-fatal): {e}")
        finally:
            _advisor_running = False

    # Consume the manual trigger only now that the run is actually starting.
    if forced:
        db.write_config('advisor_run_now', '')
    threading.Thread(target=_run, daemon=True, name='advisor').start()


_profiles_kick_date = None


def _maybe_kick_weekly_profiles() -> None:
    """Post-close slot (15:40–17:00 IST) for the weekly profile builder —
    ~100 paced historical fetches that must not run next to the live loop.
    The builder itself dedups per ISO week; this just gives it a call site
    with a valid same-day token. Once-per-day attempt, daemon thread."""
    global _profiles_kick_date
    try:
        now = datetime.now(IST)
        today = now.date().isoformat()
        m = now.hour * 60 + now.minute
        if _profiles_kick_date == today or not (15 * 60 + 40) <= m <= (17 * 60):
            return
        _profiles_kick_date = today
        token = db.get_enc_token()
        if not token or not _token_is_live(token):
            return

        def _run():
            try:
                import data_jobs
                from market_data import MarketData
                data_jobs.maybe_weekly_profiles(MarketData(KiteClient(token)))
            except Exception as e:
                print(f"[SCHEDULER] weekly profiles kick failed (non-fatal): {e}")

        threading.Thread(target=_run, daemon=True, name='profiles').start()
    except Exception as e:
        print(f"[SCHEDULER] profiles kick errored (non-fatal): {e}")


_timeline_capture_slots = set()      # (date, phase) SUCCESSFULLY captured
_timeline_capture_inflight = set()   # (date, phase) a thread is running now


def _maybe_capture_timeline() -> None:
    """Per-stock agent (P2) — always-on timeline capture in the two windows
    the advisor refresh loop doesn't cover: PRE_OPEN (08:45–09:14) and
    POST_CLOSE (15:35–16:30) IST. The intraday window is already captured by
    the advisor refresh (hourly-deduped). Once per (day, phase) when a live
    token exists; daemon thread; non-fatal. Read-only w.r.t. orders."""
    if config.QA_MODE:
        return
    try:
        now = datetime.now(IST)
        today = now.date().isoformat()
        if now.weekday() > 4 or today in config.NSE_HOLIDAYS:
            return
        m = now.hour * 60 + now.minute
        if (8 * 60 + 45) <= m <= (9 * 60 + 14):
            phase = 'PRE_OPEN'
        elif (15 * 60 + 35) <= m <= (16 * 60 + 30):
            phase = 'POST_CLOSE'
        else:
            return
        key = (today, phase)
        if key in _timeline_capture_slots or key in _timeline_capture_inflight:
            return
        token = db.get_enc_token()
        if not token or not _token_is_live(token):
            print(f"[SCHEDULER] timeline capture ({phase}) skipped — "
                  f"no live token in the window")
            return
        _timeline_capture_inflight.add(key)

        def _run():
            try:
                import portfolio_advisor
                from market_data import MarketData
                n = portfolio_advisor.run_timeline_capture(
                    MarketData(KiteClient(token)))
                _timeline_capture_slots.add(key)   # mark done only on success
                print(f"[SCHEDULER] timeline capture ({phase}) done: "
                      f"{n} holdings")
            except Exception as e:
                # NOT marked done → retries on the next tick within the window.
                print(f"[SCHEDULER] timeline capture ({phase}) failed "
                      f"(will retry): {e}")
            finally:
                _timeline_capture_inflight.discard(key)

        threading.Thread(target=_run, daemon=True, name='timeline').start()
    except Exception as e:
        print(f"[SCHEDULER] timeline capture errored (non-fatal): {e}")


_candle_backfill_days = set()      # run_dates SUCCESSFULLY backfilled
_candle_backfill_inflight = set()  # run_dates a thread is running now


def _maybe_backfill_candles() -> None:
    """[C5] post-close candle backfill, 15:40–16:30 IST, once per day.

    The in-cycle archive writes only the trailing 3 bars for symbols analyzed
    that cycle, so a position closing between cycles never gets its final bars
    written — [P-30] found 10 of 118 clean-exit trades exit past the last
    archived bar. This re-reads the whole day for everything traded.

    Deliberately here and not in the close path: an archive call on the exit
    path is the documented ~7s/cycle latency regression, and a slow exit path
    fills stops at −2.78R instead of ≈−1R. Post-close, latency is free.

    Same shape as _maybe_capture_timeline: day-gated, needs a live token,
    daemon thread, marked done only on success so a failure retries within the
    window. Starts after that job's 15:35 window opens so the two don't
    contend for the token check on the same tick.
    """
    if config.QA_MODE:
        return
    try:
        now = datetime.now(IST)
        today = now.date().isoformat()
        if now.weekday() > 4 or today in config.NSE_HOLIDAYS:
            return
        m = now.hour * 60 + now.minute
        if not ((15 * 60 + 40) <= m <= (16 * 60 + 30)):
            return
        if today in _candle_backfill_days or today in _candle_backfill_inflight:
            return
        token = db.get_enc_token()
        if not token or not _token_is_live(token):
            print('[SCHEDULER] candle backfill skipped — no live token')
            return
        _candle_backfill_inflight.add(today)

        def _run():
            try:
                import data_jobs
                from market_data import MarketData
                n = data_jobs.archive_traded_day_candles(
                    MarketData(KiteClient(token)),
                    db.get_config('active_session_id'),
                    today,
                )
                _candle_backfill_days.add(today)
                print(f"[SCHEDULER] candle backfill done: {n} bars")
            except Exception as e:
                print(f"[SCHEDULER] candle backfill failed (will retry): {e}")
            finally:
                _candle_backfill_inflight.discard(today)

        threading.Thread(target=_run, daemon=True, name='candle-backfill').start()
    except Exception as e:
        print(f"[SCHEDULER] candle backfill errored (non-fatal): {e}")


_activity_pruned_days = set()


def _maybe_prune_activity() -> None:
    """Retention for the live-feed table, once per day after 17:00 IST. [P-38]

    brain_activity is write-only in practice -- every reader is
    `order by created_at desc limit N`, and nothing reads history. At ~4,500
    rows/day that is ~1.1M rows / ~434MB a year that no query touches.

    Post-close and day-gated for the same reason every other job here is: a
    delete competing with a live trading cycle is not worth the saving. Never
    raises.
    """
    if config.QA_MODE:
        return
    try:
        now = datetime.now(IST)
        today = now.date().isoformat()
        if now.hour < 17 or today in _activity_pruned_days:
            return
        _activity_pruned_days.add(today)
        removed = db.prune_activity(90)
        print(f"[SCHEDULER] activity retention: removed {removed} rows")
    except Exception as e:
        print(f"[SCHEDULER] activity retention errored (non-fatal): {e}")


def _report_stale_token(missing: bool = False) -> None:
    """One durable token_incident + feed line per token episode (deduped).

    `missing=True` is the NO-token-at-all case, which used to write only a
    heartbeat and therefore left no trace at all ([C1]). That asymmetry is what
    made 2026-08-07 invisible: autopilot fired on time at 09:30, found no token,
    and retried every 30s until 12:40 — ~380 attempts costing ~55% of the day's
    tape — while `brain_activity` stayed empty 03:30–07:11 UTC and nothing
    durable was written. A stale token got a dashboard banner; a missing one got
    silence, even though it is the more common failure and costs just as much.

    Both cases now land in the same `token_incident` key, so the clear path
    below (and the watchdog's P1 check) covers them without knowing which
    happened.
    """
    global _stale_token_reported
    _set_heartbeat(
        'ERROR', 0,
        'No token — paste an enctoken to start' if missing
        else 'Token expired — paste a fresh enctoken to start')
    db.write_config('brain_status', 'IDLE')
    if _stale_token_reported:
        return
    _stale_token_reported = True
    try:
        # Durable flag the watchdog (P1) and dashboard read. No brain_activity
        # feed line — that table's session_id is required and there's no
        # session here (refusing to create one is the whole point).
        db.write_config(
            'token_incident',
            f"{datetime.now(IST).isoformat()} "
            f"{'no token at start' if missing else 'token stale at start'} — "
            f"awaiting fresh enctoken (no session created)",
        )
    except Exception:
        pass


def _lock_lost() -> bool:
    """True only on POSITIVE evidence a newer instance claimed the lock.
    Read errors fail open — a transient DB blip must not kill the brain."""
    try:
        owner = db.get_config_strict('brain_instance_id')
    except Exception as e:
        print(f"[SCHEDULER] instance lock check failed (ignoring): {e}")
        return False
    return bool(owner) and owner != INSTANCE_ID


def _session_still_active(session_id: str) -> bool:
    """True unless we can POSITIVELY see the session was disowned
    (active_session_id readable and different). get_config's None-on-error
    used to make one transient Supabase failure look like an external stop,
    squaring off and ending a healthy session mid-day."""
    for attempt in range(3):
        try:
            return db.get_config_strict('active_session_id') == session_id
        except Exception as e:
            print(
                f"[SCHEDULER] active_session_id check failed "
                f"({attempt + 1}/3): {e}"
            )
            time.sleep(2)
    print("[SCHEDULER] active_session_id unreadable — assuming still active")
    return True


def _config_from_session_row(row: dict) -> dict:
    """Rebuild the effective session config from the immutable session row.
    On resume this — not the mutable session_config app_config key — is the
    source of truth, so a mid-session config write can never change a
    running session's tunables (REQ-004/031)."""
    return {
        'capitalDeployed': float(row.get('capital_deployed') or 0),
        'maxTrades': int(row.get('max_trades') or 25),
        'maxLossPercent': float(row.get('max_loss_percent') or 5),
        'maxProfitPercent': float(row.get('max_profit_percent') or 15),
        'tradeIntervalSeconds': int(row.get('trade_interval_seconds') or 300),
        'stockUniverse': str(row.get('stock_universe') or 'BOTH'),
    }


def _validate_session_config(cfg: dict) -> str:
    """REQ-005 sanity checks at load. Returns an error string, or '' if OK."""
    capital = float(cfg.get('capitalDeployed') or 0)
    max_loss_pct = float(cfg.get('maxLossPercent') or 0)

    if capital <= 0:
        return f"capitalDeployed must be > 0 (got {capital})"

    # The operational 3R stop must sit inside the floor, or the floor would
    # fire first on a normal day (REQ-003 inverts: floor firing = incident).
    daily_stop_pct = config.DAILY_STOP_R * config.RISK_PER_TRADE_PCT
    if max_loss_pct < daily_stop_pct:
        return (
            f"maxLossPercent {max_loss_pct}% is inside the operational "
            f"daily stop ({config.DAILY_STOP_R}R = {daily_stop_pct}%) — "
            f"floor must be the outer boundary"
        )

    # Minimum position size must not force per-trade risk above budget.
    # Conservative assumption: a stop ~2% away from entry on a min-size
    # position; that risk must fit in risk_per_trade_pct of capital.
    min_pos_risk = config.MIN_POSITION_VALUE * 0.02
    risk_budget = capital * config.RISK_PER_TRADE_PCT / 100
    if min_pos_risk > risk_budget:
        return (
            f"capital ₹{capital:.0f} too small: min position "
            f"₹{config.MIN_POSITION_VALUE} at a 2% stop risks "
            f"₹{min_pos_risk:.0f} > per-trade budget ₹{risk_budget:.0f}"
        )
    return ''


def _handle_sigterm(signum, frame):
    # Railway redeploy/restart. Exit fast WITHOUT tearing the session down:
    # the replacement process sees status RUNNING and resumes it. Squaring
    # off here would race the new instance.
    print("[SCHEDULER] SIGTERM — exiting; replacement instance will resume the session")
    sys.exit(0)

# Shared heartbeat state written by trade loop, read by heartbeat thread
_heartbeat_status = 'ONLINE'
_heartbeat_cycle = 0
_heartbeat_message = 'Waiting for START command'
_heartbeat_lock = threading.Lock()


def _set_heartbeat(status: str, cycle: int, message: str) -> None:
    global _heartbeat_status, _heartbeat_cycle, _heartbeat_message
    with _heartbeat_lock:
        _heartbeat_status = status
        _heartbeat_cycle = cycle
        _heartbeat_message = message


def _heartbeat_thread() -> None:
    """Daemon thread: pings DB every 30s regardless of trade interval."""
    while True:
        try:
            with _heartbeat_lock:
                s, c, m = _heartbeat_status, _heartbeat_cycle, _heartbeat_message
            # Error budget: repeated control-plane failures surface as
            # DEGRADED so the external watchdog alerts even while the
            # heartbeat write itself still succeeds.
            if db.health_degraded() and s in ('ONLINE', 'RUNNING'):
                s = 'DEGRADED'
                m = f"{m} | control-plane DB errors ≥{db.DEGRADED_THRESHOLD} consecutive"
            db.update_heartbeat(s, c, m)
        except Exception as e:
            print(f"[HEARTBEAT] error: {e}")
        # 30s is the heartbeat cadence the EXTERNAL WATCHDOG is calibrated to:
        # watchdog.HEARTBEAT_STALE_SECONDS = 150 means '5 missed writes = dead'.
        # Changing this number silently re-scales that alarm -- at 60s it would
        # fire after 2.5 misses, at 15s after 10. Change both together.
        time.sleep(30)


def _should_autostart() -> bool:
    """Autopilot gate: trading day, inside the 09:30-15:20 window, and no
    session created yet today. Any session today — manual stop, loss limit,
    token expiry — suppresses restart until tomorrow."""
    if not config.AUTOPILOT:
        return False

    now = datetime.now(IST)
    if now.weekday() > 4 or now.strftime('%Y-%m-%d') in config.NSE_HOLIDAYS:
        return False

    start = now.replace(
        hour=config.MARKET_START_TRADING_HOUR,
        minute=config.MARKET_START_TRADING_MINUTE,
        second=0, microsecond=0,
    )
    close = now.replace(
        hour=config.MARKET_CLOSE_HOUR,
        minute=config.MARKET_CLOSE_MINUTE,
        second=0, microsecond=0,
    )
    if not (start <= now < close):
        return False

    return not db.has_session_today()


def run():
    global _is_trading, _stale_token_reported

    print(
        f"Zerodha Brain v1.0.0 started at "
        f"{datetime.now(IST).strftime('%Y-%m-%d %H:%M:%S IST')} "
        f"(instance {INSTANCE_ID[:8]})"
    )

    try:
        signal.signal(signal.SIGTERM, _handle_sigterm)
    except ValueError:
        pass  # not the main thread (tests)

    # Claim the instance lock — any older process still running sees this
    # and exits instead of double-trading the session.
    db.write_config('brain_instance_id', INSTANCE_ID)

    # Start background heartbeat — fires every 30s independent of trade sleep
    t = threading.Thread(target=_heartbeat_thread, daemon=True, name='heartbeat')
    t.start()

    # Decision bot (records Accept/Decline taps; NO order path) — same rules.
    try:
        import advisor_bot
        advisor_bot.start_advisor_bot()
    except Exception as e:
        print(f"[SCHEDULER] advisor bot failed to start (non-fatal): {e}")

    db.update_heartbeat('ONLINE', 0, 'Brain started, waiting for command')

    while True:
        try:
            _set_heartbeat('ONLINE', 0, 'Idle — advisor only (intraday decommissioned 2026-09-09)')
            # Weekly profiles get their post-close window (15:40+).
            _maybe_kick_weekly_profiles()
            # AMFI NAV ingestion (public, no token) — prices NAV-based holdings.
            _maybe_ingest_amfi_nav()
            # Retention for the write-only live-feed table ([P-38]).
            _maybe_prune_activity()
            # Portfolio advisor: once per day when a live token exists.
            # Advisory-only, independent of trading sessions; never blocks
            # the loop (runs on a daemon thread).
            # Daily-bar store runs FIRST on a live token; the advisor and
            # grading wait until it finishes (minutes, once per day).
            if not _maybe_sync_daily_bars():
                _maybe_run_advisor()
                # Grading + labelling catch-ups, hoisted here from the removed
                # session-start path so the two pre-registered advisor reads
                # keep accruing. Once/day, so the idle loop never re-spawns.
                _maybe_run_catchups()
                # Second-account preview (P-45 provisional, own token).
                _maybe_run_account_previews()
            # On-demand single-stock lookup (pull-mode, needs a token).
            _maybe_serve_lookup()
            command = db.get_brain_command()

            # brain_status flips START -> RUNNING right after session creation
            # and stays RUNNING for the rest of the session — it is never set
            # back to START. A brain restart (crash, Railway redeploy) mid-
            # session is therefore a fresh process reading command=='RUNNING',
            # which used to match neither branch: the brain would idle forever
            # while the old session sat stuck RUNNING with no new trades — a
            # silent failure indistinguishable from normal operation on the
            # dashboard. Treat RUNNING the same as START, and resume the
            # existing session instead of creating a duplicate.
            # ── Intraday trading removed 2026-09-09 (decommission Phase C.2/C.3).
            # The START/RUNNING session branch — session creation, brain
            # initialization, brain.run_cycle(), the in-session advisor tick,
            # intraday exit checks and EOD teardown — is deleted. A START
            # command is now inert; the advisor runs from the idle loop above,
            # independent of any session. git history is the reversibility.
            if command == 'STOP':

                # STOP while nothing is trading (brain restarted into an
                # error state, or a crash raced teardown). Only the trade
                # loop used to acknowledge STOP, so this left brain_status
                # stuck on STOP and a ghost session RUNNING forever with the
                # dashboard unable to reset. Tear down whatever is left.
                stale_id = db.get_config('active_session_id')
                if stale_id:
                    sess = db.get_session_by_id(stale_id)
                    if sess and sess.get('status') == 'RUNNING':
                        print(f"[SCHEDULER] STOP while idle — finalizing orphaned session {stale_id}")
                        # No brain/positions context here; open trades get
                        # STALE_CLEANUP'd by the next session's initialize.
                        db.end_session(stale_id, 'MANUAL_STOP')
                db.write_config('active_session_id', '')
                db.write_config('brain_status', 'IDLE')
                time.sleep(5)

            else:
                time.sleep(30)

        except KeyboardInterrupt:
            print("Brain stopped manually")
            _set_heartbeat('OFFLINE', 0, 'Stopped manually')
            break

        except Exception as e:
            print(f"Scheduler error: {e}")
            _set_heartbeat('ERROR', 0, f"Error: {str(e)}")
            _is_trading = False
            time.sleep(60)
