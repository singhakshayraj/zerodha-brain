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
_last_heartbeat = 0.0


def _maybe_heartbeat(status: str, cycle: int, message: str) -> None:
    global _last_heartbeat
    now = time.time()
    if now - _last_heartbeat >= 55:
        db.update_heartbeat(status, cycle, message)
        _last_heartbeat = now


def run():
    global _is_trading

    print(
        f"Zerodha Brain v1.0.0 started at "
        f"{datetime.now(IST).strftime('%Y-%m-%d %H:%M:%S IST')}"
    )

    db.update_heartbeat('ONLINE', 0, 'Brain started, waiting for command')
    _last_heartbeat_ref = [time.time()]  # track so first loop doesn't double-ping

    while True:
        try:
            _maybe_heartbeat('ONLINE', 0, 'Waiting for START command')
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
                        db.update_heartbeat('ERROR', 0, 'No token — reconnect from app')
                        time.sleep(30)
                        continue

                    if not session_config:
                        print("[SCHEDULER] No session config found")
                        db.update_heartbeat('ERROR', 0, 'No session config found')
                        time.sleep(30)
                        continue

                    print("[SCHEDULER] Creating session in DB...")
                    session = db.create_session(session_config)

                    if not session:
                        print("[SCHEDULER] CRITICAL: Session creation failed")
                        db.update_heartbeat('ERROR', 0, 'DB session creation failed')
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
                        db.update_heartbeat('ERROR', 0, 'Initialization failed')
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

                        _maybe_heartbeat(
                            'RUNNING',
                            brain.session_stats['trades_executed'],
                            f"Active | P&L: ₹{brain.session_stats['total_pnl']:.2f}",
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
            db.update_heartbeat('OFFLINE', 0, 'Stopped manually')
            break

        except Exception as e:
            print(f"Scheduler error: {e}")
            db.update_heartbeat('ERROR', 0, f"Error: {str(e)}")
            _is_trading = False
            time.sleep(60)
