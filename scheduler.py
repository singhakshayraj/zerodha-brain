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


# Portfolio advisor dedup: the ISO date it last ran, so it fires once per day.
_advisor_date = None
_advisor_running = False
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
        print("[SCHEDULER] token preflight FAILED — alerting")
        if config.ADVISOR_TELEGRAM_BOT_TOKEN and config.ADVISOR_TELEGRAM_CHAT_ID:
            telegram.send_message(
                config.ADVISOR_TELEGRAM_BOT_TOKEN,
                config.ADVISOR_TELEGRAM_CHAT_ID,
                f"⚠️ Kite session expired/missing. Paste a fresh enc_token "
                f"before the {run_at} IST advisor run!")
    except Exception as e:
        print(f"[SCHEDULER] token preflight errored (non-fatal): {e}")


def _maybe_run_advisor() -> None:
    """Daily portfolio advisory (ADVISORY ONLY — no orders). Fires once per
    day after ADVISOR_RUN_AFTER_IST (09:45 — past the opening-bell noise
    window) when a token exists and probes live. A manual trigger
    (app_config 'advisor_run_now'=='true') bypasses the time gate and the
    once-per-day dedup, for an on-demand run outside the window — set from the
    dashboard or directly in app_config; cleared immediately so it fires once.
    Runs on a daemon thread so the scheduler loop never waits on candle
    fetches. Skipped in QA (synthetic market has no real holdings)."""
    global _advisor_date, _advisor_running
    if config.QA_MODE or _advisor_running:
        return
    now = datetime.now(IST)
    today = now.date().isoformat()

    forced = (db.get_config('advisor_run_now') or '').strip().lower() == 'true'
    if forced:
        db.write_config('advisor_run_now', '')  # consume — fires once
    elif _advisor_date == today:
        return
    else:
        rh, rm = _parse_hhmm(config.ADVISOR_RUN_AFTER_IST, (9, 45))
        if (now.hour, now.minute) < (rh, rm):
            return

    token = db.get_enc_token()
    if not token or not _token_is_live(token):
        return
    _advisor_date = today
    _advisor_running = True

    def _run():
        global _advisor_running
        try:
            import portfolio_advisor
            from market_data import MarketData
            md = MarketData(KiteClient(token))
            n = portfolio_advisor.run_advisor(md)
            print(f"[SCHEDULER] advisor done: {n} holdings analyzed")
            if config.ADVISOR_BACKTEST_ENABLED:
                try:
                    import advisor_backtest
                    advisor_backtest.run_backtest_pass(md)
                except Exception as e:
                    print(f"[SCHEDULER] advisor backtest failed "
                          f"(non-fatal): {e}")
        except Exception as e:
            print(f"[SCHEDULER] advisor failed (non-fatal): {e}")
        finally:
            _advisor_running = False

    threading.Thread(target=_run, daemon=True, name='advisor').start()


def _report_stale_token() -> None:
    """One durable token_incident + feed line per stale episode (deduped)."""
    global _stale_token_reported
    _set_heartbeat('ERROR', 0, 'Token expired — paste a fresh enctoken to start')
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
            f"{datetime.now(IST).isoformat()} token stale at start — "
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

    # Intraday holdings watch (ADVISORY ONLY) — no-op unless enabled + keyed.
    try:
        import advisor_watch
        advisor_watch.start_advisor_watch()
    except Exception as e:
        print(f"[SCHEDULER] advisor watch failed to start (non-fatal): {e}")

    db.update_heartbeat('ONLINE', 0, 'Brain started, waiting for command')

    while True:
        try:
            _set_heartbeat('ONLINE', 0, 'Waiting for START command')
            # Daily 6:30 IST token refresh — no-op unless KITE_* creds set.
            token_refresher.maybe_daily_refresh()
            # Token preflight (09:16): a dead enc_token is still fixable
            # before the 09:45 advisor run — alert, don't silently skip.
            _maybe_token_preflight()
            # Portfolio advisor: once per day when a live token exists.
            # Advisory-only, independent of trading sessions; never blocks
            # the loop (runs on a daemon thread).
            _maybe_run_advisor()
            command = db.get_brain_command()

            if command not in ('START', 'RUNNING') and not _is_trading and _should_autostart():
                print("[AUTOPILOT] Trading day, 09:30 window, no session yet — self-starting")
                db.write_config('brain_status', 'START')
                command = 'START'

            # brain_status flips START -> RUNNING right after session creation
            # and stays RUNNING for the rest of the session — it is never set
            # back to START. A brain restart (crash, Railway redeploy) mid-
            # session is therefore a fresh process reading command=='RUNNING',
            # which used to match neither branch: the brain would idle forever
            # while the old session sat stuck RUNNING with no new trades — a
            # silent failure indistinguishable from normal operation on the
            # dashboard. Treat RUNNING the same as START, and resume the
            # existing session instead of creating a duplicate.
            if command in ('START', 'RUNNING'):
                if _is_trading:
                    print("[SCHEDULER] Already trading, ignoring START")
                    time.sleep(5)
                    continue

                _is_trading = True
                try:
                    print(f"{command} command received")

                    token = db.get_enc_token()
                    if config.QA_MODE and not token:
                        token = 'QA-DUMMY'  # FakeKiteClient never uses it
                    session_config = db.get_session_config()

                    print(f"[SCHEDULER] Token exists: {bool(token)}")
                    print(f"[SCHEDULER] Session config: {session_config}")

                    if not token:
                        print("[SCHEDULER] No token found — attempting auto-refresh")
                        token = token_refresher.refresh_enc_token()

                    if not token:
                        print("[SCHEDULER] No token found")
                        _set_heartbeat('ERROR', 0, 'No token — reconnect from app')
                        time.sleep(30)
                        continue

                    if not session_config:
                        print("[SCHEDULER] No session config found")
                        _set_heartbeat('ERROR', 0, 'No session config found')
                        time.sleep(30)
                        continue

                    existing_id = db.get_config('active_session_id')
                    existing = db.get_session_by_id(existing_id) if existing_id else None

                    is_resume = bool(existing and existing.get('status') == 'RUNNING')

                    if is_resume:
                        session_id = existing_id
                        print(f"[SCHEDULER] Resuming existing RUNNING session {session_id} (brain restart)")
                        # Immutable config: rebuild from the session row, not
                        # the mutable session_config key (REQ-004/031).
                        session_config = _config_from_session_row(existing)
                        session_config['configHash'] = existing.get('config_hash')

                        # REQ-072 deploy-freeze guard: a resume under a
                        # different SHA means code changed mid-session
                        # (push during market hours). Resume anyway — safety
                        # first — but raise a watchdog-visible incident.
                        old_sha = existing.get('git_sha')
                        if old_sha and old_sha != config.GIT_SHA:
                            msg = (
                                f"code changed mid-session: {old_sha} -> "
                                f"{config.GIT_SHA} (session {session_id})"
                            )
                            # Once per (session, sha) pair per process. On
                            # 2026-07-08 a failing resume + autopilot retry
                            # loop re-entered this path every ~40s, re-writing
                            # the incident and spamming the feed for an hour.
                            dedup_key = (session_id, config.GIT_SHA)
                            if dedup_key not in _reported_deploy_incidents:
                                _reported_deploy_incidents.add(dedup_key)
                                print(f"[SCHEDULER] REQ-072 INCIDENT: {msg}")
                                db.write_config(
                                    'deploy_incident',
                                    f"{datetime.now(IST).isoformat()} {msg}",
                                )
                                db.log_brain_activity(
                                    session_id, 'ERROR',
                                    message=f"[REQ-072] {msg}",
                                )
                    else:
                        err = _validate_session_config(session_config)
                        if err:
                            print(f"[SCHEDULER] Config sanity check FAILED: {err}")
                            _set_heartbeat('ERROR', 0, f"Config rejected: {err}")
                            db.write_config('brain_status', 'IDLE')
                            time.sleep(30)
                            continue

                        # Probe the token BEFORE creating a session. A stale
                        # token here must not spawn a doomed session — that row
                        # trips has_session_today(), which suppresses autopilot
                        # for the rest of the day (the 2026-07-09 lost morning).
                        # Refusing to create it keeps has_session_today() False,
                        # so autopilot retries every tick and starts the instant
                        # a fresh token is pasted.
                        if not _token_is_live(token):
                            print("[SCHEDULER] Token not live — awaiting fresh token, no session created")
                            _report_stale_token()
                            time.sleep(30)
                            continue

                        # Token proved live. Clear any stale-token incident NOW —
                        # before the ~44s brain.initialize() — so the dashboard
                        # banner doesn't flash on a healthy start (it used to
                        # linger until the post-init clear below).
                        if _stale_token_reported:
                            _stale_token_reported = False
                            db.write_config('token_incident', '')

                        print("[SCHEDULER] Creating session in DB...")
                        session = db.create_session(session_config)

                        if not session:
                            print("[SCHEDULER] CRITICAL: Session creation failed")
                            _set_heartbeat('ERROR', 0, 'DB session creation failed')
                            db.write_config('brain_status', 'IDLE')
                            time.sleep(30)
                            continue

                        session_id = session['id']
                        print(f"[SCHEDULER] Session ready: {session_id}")
                        session_config['configHash'] = session.get('config_hash')
                        db.write_config('active_session_id', session_id)

                    session_config['sessionId'] = session_id

                    db.write_config('brain_status', 'RUNNING')
                    # Dashboard reads this to label paper vs real sessions.
                    db.write_config(
                        'paper_mode',
                        'true' if config.PAPER_TRADING else 'false',
                    )

                    brain = TradingBrain()
                    initialized = brain.initialize(token, session_config)
                    if initialized and is_resume:
                        brain.resume_stats(session_id)
                    if initialized:
                        # Token proved live — clear any stale-token incident so
                        # the watchdog stops alerting and the next stale episode
                        # reports fresh.
                        if _stale_token_reported:
                            _stale_token_reported = False
                            db.write_config('token_incident', '')

                    if not initialized:
                        print("Brain initialization failed")
                        # Release the session pointers. Before this, a failed
                        # init (e.g. an expired token at 09:30 autopilot start)
                        # set IDLE but left the DB row RUNNING and
                        # active_session_id pointing at it — a zombie that
                        # silently blocked every later Start (2026-07-09).
                        try:
                            db.update_session(session_id, {
                                'status': 'ABORTED',
                                'end_reason': 'INIT_FAILED',
                                'ended_at': datetime.now(IST).isoformat(),
                            })
                        except Exception as e:
                            print(f"[SCHEDULER] abort of {session_id} failed: {e}")
                        db.write_config('active_session_id', '')
                        db.write_config('brain_status', 'IDLE')
                        _set_heartbeat('ERROR', 0, 'Initialization failed')
                        time.sleep(30)
                        continue

                    interval = session_config.get('tradeIntervalSeconds', 300)
                    print(f"Brain running. Interval: {interval}s")

                    def _end(reason: str, call_end_session: bool = True) -> None:
                        # The brain owns the whole session teardown: square-off
                        # + stats via end_session, then release the pointers the
                        # dashboard keys off. Leaving active_session_id set
                        # after MARKET_CLOSED/loss-limit ends caused stale
                        # "running" UIs and resurrection bugs.
                        if call_end_session:
                            brain.end_session(reason)
                        db.write_config('active_session_id', '')
                        db.write_config('brain_status', 'IDLE')

                    while True:
                        command = db.get_brain_command()

                        if command == 'STOP':
                            print("STOP command received")
                            _end('MANUAL_STOP')
                            break

                        if not risk_manager.is_market_open():
                            print("Market closed. Ending session.")
                            _end('MARKET_CLOSED')
                            break

                        if brain._session_ended:
                            print("[SCHEDULER] Brain ended session internally.")
                            _end('INTERNAL', call_end_session=False)
                            break

                        # A STOP can be overwritten by a quick START while we
                        # sleep between cycles (single mutable command key) —
                        # observed live 2026-07-06: stop→start within seconds
                        # left the brain trading a ghost session the dashboard
                        # couldn't see. Re-verify we're still the active one.
                        if not _session_still_active(session_id):
                            print(
                                f"[SCHEDULER] Session {session_id} no longer "
                                f"active — ending"
                            )
                            _end('EXTERNAL_STOP')
                            break

                        # A newer instance (redeploy) owns the session now —
                        # exit WITHOUT teardown so we don't double-trade or
                        # square off under its feet.
                        if _lock_lost():
                            print("[SCHEDULER] Instance lock lost — exiting, newer instance owns the session")
                            sys.exit(0)

                        _set_heartbeat(
                            'RUNNING',
                            brain.cycle_count,
                            f"Cycle {brain.cycle_count} | "
                            f"Trades: {brain.session_stats['trades_executed']} | "
                            f"P&L: ₹{brain.session_stats['total_pnl']:.2f}",
                        )

                        brain.run_cycle()

                        # Sleep in short slices so a STOP is obeyed in seconds,
                        # not at the next 5-minute cycle boundary — the blind
                        # 300s sleep was the race window behind the lost-STOP
                        # ghost session.
                        slept = 0
                        tick = 0
                        while slept < interval:
                            time.sleep(min(10, interval - slept))
                            slept += 10
                            tick += 1

                            # Intra-cycle stop/target enforcement (~every
                            # 30s). Cycle-boundary-only checks let stops fill
                            # at −2.78R instead of ≈−1R (2026-07-08 data).
                            if tick % 3 == 0:
                                try:
                                    brain.check_open_exits()
                                except Exception as e:
                                    print(f"[SCHEDULER] exit check error: {e}")
                                if brain._session_ended:
                                    break  # circuit breaker fired mid-slice

                            cmd = db.get_brain_command()
                            if (
                                cmd == 'STOP'
                                or not _session_still_active(session_id)
                                or _lock_lost()
                            ):
                                print(f"[SCHEDULER] Wake early: cmd={cmd}")
                                break

                finally:
                    _is_trading = False

            elif command == 'STOP':
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
