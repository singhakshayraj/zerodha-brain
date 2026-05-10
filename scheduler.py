import time
from datetime import datetime

import pytz

import database as db
from brain import TradingBrain
from risk_manager import RiskManager

IST = pytz.timezone('Asia/Kolkata')
risk_manager = RiskManager()


def run():
    print(
        f"Zerodha Brain v1.0.0 started at "
        f"{datetime.now(IST).strftime('%Y-%m-%d %H:%M:%S IST')}"
    )

    db.update_heartbeat('ONLINE', 0, 'Brain started, waiting for command')

    while True:
        try:
            db.update_heartbeat('ONLINE', 0, 'Waiting for START command')
            command = db.get_brain_command()

            if command == 'START':
                print("START command received")

                token = db.get_enc_token()
                session_config = db.get_session_config()

                if not token:
                    print("No enc_token found in config")
                    db.update_heartbeat(
                        'ERROR', 0,
                        'No token found. Please connect from the app.',
                    )
                    time.sleep(30)
                    continue

                if not session_config:
                    print("No session config found")
                    db.update_heartbeat('ERROR', 0, 'No session config found')
                    time.sleep(30)
                    continue

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

                    db.update_heartbeat(
                        'RUNNING',
                        brain.session_stats['trades_executed'],
                        f"Active | P&L: ₹{brain.session_stats['total_pnl']:.2f}",
                    )

                    brain.run_cycle()

                    print(f"Sleeping {interval}s until next cycle...")
                    time.sleep(interval)

            else:
                time.sleep(30)

        except KeyboardInterrupt:
            print("Brain stopped manually")
            db.update_heartbeat('OFFLINE', 0, 'Stopped manually')
            break

        except Exception as e:
            print(f"Scheduler error: {e}")
            db.update_heartbeat('ERROR', 0, f"Error: {str(e)}")
            time.sleep(60)
