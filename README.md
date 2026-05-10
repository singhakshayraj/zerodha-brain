# Zerodha Brain

Autonomous trading brain for zerodha-trader app.
Runs on Railway 24/7.
Reads commands from Supabase.
Executes trades via Zerodha Kite API.

## Environment Variables (set in Railway)
SUPABASE_URL=
SUPABASE_SERVICE_KEY=

## How it works
1. Deploy this repo to Railway
2. Set environment variables
3. Open zerodha-trader app
4. Paste enc_token and click Connect
5. Configure and click START
6. Brain runs automatically

## Architecture
scheduler.py → brain.py → signal_engine.py
                        → order_manager.py
                        → market_data.py → kite_client.py
                        → database.py → Supabase
