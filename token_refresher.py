"""Daily Kite enc_token auto-refresh via TOTP login.

Zerodha web-session enctokens expire around 6 AM IST every day. This module
replays the same login flow the kite.zerodha.com web app uses (password +
TOTP) and stores the fresh enctoken in the Supabase config table, where the
scheduler and brain already read it from.

Auto-login is enabled only when KITE_USER_ID, KITE_PASSWORD and
KITE_TOTP_SECRET are all set (Railway env vars). Without them every function
here is a no-op and the manual paste flow from the dashboard remains the
only way to supply a token.
"""

from datetime import datetime

import pytz
import requests

import config
import database as db

IST = pytz.timezone('Asia/Kolkata')

LOGIN_URL = 'https://kite.zerodha.com/api/login'
TWOFA_URL = 'https://kite.zerodha.com/api/twofa'

# Date (IST) of the last successful auto-refresh, so the daily check fires
# at most once per day.
_last_refresh_date = None


def is_enabled() -> bool:
    return bool(
        config.KITE_USER_ID and config.KITE_PASSWORD and config.KITE_TOTP_SECRET
    )


def refresh_enc_token():
    """Log in to Kite web and write a fresh enctoken to Supabase.

    Returns the new token on success, None on any failure. Never raises —
    callers treat None as "token still missing/stale" and surface it via
    heartbeat like before.
    """
    global _last_refresh_date

    if not is_enabled():
        print("[token_refresher] disabled — KITE_USER_ID/PASSWORD/TOTP_SECRET not set")
        return None

    try:
        import pyotp

        session = requests.Session()

        resp = session.post(
            LOGIN_URL,
            data={
                'user_id': config.KITE_USER_ID,
                'password': config.KITE_PASSWORD,
            },
            timeout=15,
        )
        body = resp.json()
        if resp.status_code != 200 or body.get('status') != 'success':
            print(f"[token_refresher] login failed: {resp.status_code} {body.get('message')}")
            return None

        request_id = body['data']['request_id']

        resp = session.post(
            TWOFA_URL,
            data={
                'user_id': config.KITE_USER_ID,
                'request_id': request_id,
                'twofa_value': pyotp.TOTP(config.KITE_TOTP_SECRET).now(),
                'twofa_type': 'totp',
            },
            timeout=15,
        )
        if resp.status_code != 200 or resp.json().get('status') != 'success':
            print(f"[token_refresher] twofa failed: {resp.status_code} {resp.text[:200]}")
            return None

        token = session.cookies.get('enctoken')
        if not token:
            print("[token_refresher] twofa OK but no enctoken cookie in response")
            return None

        db.write_config('enc_token', token)
        _last_refresh_date = datetime.now(IST).date()
        print(f"[token_refresher] enc_token refreshed OK at {datetime.now(IST).strftime('%H:%M:%S IST')}")
        return token

    except Exception as e:
        print(f"[token_refresher] error: {e}")
        return None


def maybe_daily_refresh():
    """Refresh once per day after the expiry window (called from the
    scheduler idle loop). Old tokens die ~6 AM IST; refreshing at
    TOKEN_REFRESH_HOUR_IST guarantees a live token before 9:15 open."""
    if not is_enabled():
        return None

    now = datetime.now(IST)
    past_window = (now.hour, now.minute) >= (
        config.TOKEN_REFRESH_HOUR_IST,
        config.TOKEN_REFRESH_MINUTE_IST,
    )
    if past_window and _last_refresh_date != now.date():
        print("[token_refresher] daily refresh window hit")
        return refresh_enc_token()
    return None
