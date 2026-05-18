import sys
sys.path.insert(0, '.')

import database as db
from kite_client import KiteClient

# Fetch enctoken from Supabase
token = db.get_enc_token()
print(f"[test] Token found: {bool(token)}")

if not token:
    print("[test] ❌ No token in Supabase — set enc_token in brain_config table")
    sys.exit(1)

# Init kite
kite = KiteClient(token)

# Verify auth
try:
    profile = kite.get_profile()
    print(f"[test] Auth OK — user: {profile.get('user_name', 'unknown')}")
except Exception as e:
    print(f"[test] ❌ Auth failed: {e}")
    sys.exit(1)

# Place test LIMIT order at ₹1.00 — will never fill
print("[test] Placing test AMO order...")
try:
    order_id = kite.place_order(
        symbol='INFY',
        exchange='NSE',
        transaction_type='BUY',
        quantity=1,
        order_type='LIMIT',
        product='CNC',
        price='1050.00',    # INFY ~₹1117, below market — won't fill
        variety='amo',
    )
    if order_id:
        print(f"[test] ✅ SUCCESS — order_id={order_id}")
        print(f"[test] Go to Kite → Orders → AMO to verify, then cancel it")
    else:
        print("[test] ❌ order_id is None — check logs above for error details")
except Exception as e:
    print(f"[test] ❌ FAILED: {e}")
