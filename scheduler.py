import threading
import time
from datetime import datetime

import pytz

import config
import database as db
import token_refresher
from brain import TradingBrain
from risk_manager import RiskManager

IST = pytz.timezone('Asia/Kolkata')
risk_manager = RiskManager()

_brain_lock = threading.Lock()
_is_trading = False

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
    global _is_trading

    print(
        f"Zerodha Brain v1.0.0 started at "
        f"{datetime.now(IST).strftime('%Y-%m-%d %H:%M:%S IST')}"
    )

    # Start background heartbeat — fires every 30s independent of trade sleep
    t = threading.Thread(target=_heartbeat_thread, daemon=True, name='heartbeat')
    t.start()

    db.update_heartbeat('ONLINE', 0, 'Brain started, waiting for command')

    while True:
        try:
            _set_heartbeat('ONLINE', 0, 'Waiting for START command')
            # Daily 6:30 IST token refresh — no-op unless KITE_* creds set.
            token_refresher.maybe_daily_refresh()
            command = db.get_brain_command()

            if command != 'START' and not _is_trading and _should_autostart():
                print("[AUTOPILOT] Trading day, 09:30 window, no session yet — self-starting")
                db.write_config('brain_status', 'START')
                command = 'START'

            if command == 'START':
                if _is_trading:
                    print("[SCHEDULER] Already trading, ignoring START")
                    time.sleep(5)
                    continue

                _is_trading = True
                try:
                    print("START command received")

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

                    if not initialized:
                        print("Brain initialization failed")
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
                        active_id = db.get_config('active_session_id')
                        if active_id != session_id:
                            print(
                                f"[SCHEDULER] Session {session_id} no longer "
                                f"active (active={active_id!r}) — ending"
                            )
                            _end('EXTERNAL_STOP')
                            break

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
                        while slept < interval:
                            time.sleep(min(10, interval - slept))
                            slept += 10
                            cmd = db.get_brain_command()
                            active_now = db.get_config('active_session_id')
                            if cmd == 'STOP' or active_now != session_id:
                                print(f"[SCHEDULER] Wake early: cmd={cmd} active={active_now!r}")
                                break

                finally:
                    _is_trading = False

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
