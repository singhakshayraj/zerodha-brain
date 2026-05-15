import threading
import time
from datetime import datetime

import pytz

import database as db
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
            command = db.get_brain_command()

            if command == 'START':
                if _is_trading:
                    print("[SCHEDULER] Already trading, ignoring START")
                    time.sleep(5)
                    continue

                _is_trading = True
                try:
                    print("START command received")

                    token = db.get_enc_token()
                    session_config = db.get_session_config()

                    print(f"[SCHEDULER] Token exists: {bool(token)}")
                    print(f"[SCHEDULER] Session config: {session_config}")

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

                    while True:
                        command = db.get_brain_command()

                        if command == 'STOP':
                            print("STOP command received")
                            brain.end_session('MANUAL_STOP')
                            db.write_config('brain_status', 'IDLE')
                            break

                        if not risk_manager.is_market_open():
                            print("Market closed. Ending session.")
                            brain.end_session('MARKET_CLOSED')
                            db.write_config('brain_status', 'IDLE')
                            break

                        if brain._session_ended:
                            print("[SCHEDULER] Brain ended session internally.")
                            db.write_config('brain_status', 'IDLE')
                            break

                        _set_heartbeat(
                            'RUNNING',
                            brain.cycle_count,
                            f"Cycle {brain.cycle_count} | "
                            f"Trades: {brain.session_stats['trades_executed']} | "
                            f"P&L: ₹{brain.session_stats['total_pnl']:.2f}",
                        )

                        brain.run_cycle()

                        print(f"Sleeping {interval}s until next cycle...")
                        time.sleep(interval)

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
