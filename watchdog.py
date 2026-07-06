"""External watchdog for the trading brain — Tier 1 alerting.

Runs as a SEPARATE Railway service (start command: python3 -u watchdog.py) so
it survives the exact failures it exists to detect: brain crash, crash-loop,
hang, stale heartbeat, token expiry, silent zero-trade mornings.

Checks (IST, trading days only):
  - 08:45  reminder to paste today's enc_token (manual daily step)
  - market hours: heartbeat stale > HEARTBEAT_STALE_SECONDS  → CRITICAL
  - market hours: heartbeat status ERROR / DEGRADED          → WARNING
  - market hours: brain_status TOKEN_EXPIRED                 → CRITICAL
  - 11:00+ : session active but zero trades logged today     → WARNING

Alerts go to Telegram when TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID are set;
otherwise they are printed only (so the service is safe to deploy before the
bot exists). Repeated alerts are deduplicated per key.

Env needed: SUPABASE_URL, SUPABASE_SERVICE_KEY (production project),
TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID.
"""

import os
import time
from datetime import datetime, timezone

import pytz
import requests
from supabase import create_client

import config

IST = pytz.timezone('Asia/Kolkata')

CHECK_INTERVAL_SECONDS = 60
HEARTBEAT_STALE_SECONDS = 150       # brain writes every 30s; 5 misses = dead
ALERT_REPEAT_SECONDS = 1800         # same alert at most every 30 min

TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN')
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')

# key -> unix ts of last send (in-memory; a watchdog restart may repeat an
# alert once, which is the safe direction)
_last_sent = {}


# Alert tiers (REQ-071). P1 halt-class → immediate push; P2 degraded →
# push, tagged for triage; P3 info → log/dashboard only, never paged.
P1, P2, P3 = 'P1', 'P2', 'P3'
_TELEGRAM_TIERS = {P1, P2}


def send_alert(key: str, message: str, now_ts: float = None,
               tier: str = P1) -> bool:
    """Send one deduplicated alert. Returns True if actually sent (fired),
    regardless of channel — P3 fires to the log only."""
    now_ts = now_ts if now_ts is not None else time.time()
    last = _last_sent.get(key)
    if last is not None and now_ts - last < ALERT_REPEAT_SECONDS:
        return False
    _last_sent[key] = now_ts

    print(f"[watchdog] [{tier}] {key}: {message}")
    if tier not in _TELEGRAM_TIERS:
        return True  # P3: dashboard/log only
    if not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
        print("[watchdog] (Telegram not configured — printed only)")
        return True
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={'chat_id': TELEGRAM_CHAT_ID, 'text': f"[{tier}] {message}"},
            timeout=10,
        ).raise_for_status()
    except Exception as e:
        print(f"[watchdog] Telegram send failed: {e}")
    return True


def is_trading_day(now_ist: datetime) -> bool:
    return (
        now_ist.weekday() <= 4
        and now_ist.strftime('%Y-%m-%d') not in config.NSE_HOLIDAYS
    )


def _minutes(now_ist: datetime) -> int:
    return now_ist.hour * 60 + now_ist.minute


def in_market_hours(now_ist: datetime) -> bool:
    return (9 * 60 + 15) <= _minutes(now_ist) <= (15 * 60 + 30)


def evaluate(state: dict, now_ist: datetime) -> list:
    """Pure check logic → list of (tier, key, message). Tiers per REQ-071:
    P1 halt-class, P2 degraded, P3 info. state:
    heartbeat: {'last_ping': iso str, 'status': str, 'message': str} | None
    brain_status: str | None
    trades_today: int | None   (None = query failed, skip that check)
    """
    alerts = []
    day = now_ist.strftime('%Y-%m-%d')

    if not is_trading_day(now_ist):
        return alerts

    m = _minutes(now_ist)

    # 08:45–09:10 token reminder (once per day via dedup key with date)
    if (8 * 60 + 45) <= m < (9 * 60 + 10):
        alerts.append((
            P2, f"token-reminder-{day}",
            "🔑 Reminder: paste today's enc_token into the dashboard "
            "before 09:15 IST (market opens soon).",
        ))

    # Token/deploy incidents are durable flags the brain leaves; they must
    # alert even outside market hours (a redeploy or expiry can land at the
    # boundary) — evaluated before the market-hours gate.
    token_incident = state.get('token_incident')
    if token_incident:
        alerts.append((
            P1, f"token-incident-{token_incident[:40]}",
            f"🚨 enc_token EXPIRED mid-session — session ended. Paste a fresh "
            f"token and restart. ({token_incident})",
        ))

    incident = state.get('deploy_incident')
    if incident:
        alerts.append((
            P1, f"deploy-incident-{incident[:40]}",
            f"⚠️ Deploy during session (REQ-072): {incident}",
        ))

    if not in_market_hours(now_ist):
        return alerts

    hb = state.get('heartbeat')
    if hb is None:
        alerts.append((
            P1, 'heartbeat-missing',
            '🚨 CRITICAL: no brain heartbeat row readable — brain may be '
            'down or DB unreachable.',
        ))
    else:
        try:
            last_ping = datetime.fromisoformat(
                (hb.get('last_ping') or '').replace('Z', '+00:00')
            )
            age = (datetime.now(timezone.utc) - last_ping).total_seconds()
        except Exception:
            age = None
        if age is None or age > HEARTBEAT_STALE_SECONDS:
            alerts.append((
                P1, 'heartbeat-stale',
                f"🚨 CRITICAL: brain heartbeat stale "
                f"({'unreadable' if age is None else f'{int(age)}s old'}) "
                f"during market hours — brain is down or hung. "
                f"Last status: {hb.get('status')} | {hb.get('message')}",
            ))
        elif hb.get('status') in ('ERROR', 'DEGRADED'):
            alerts.append((
                P2, f"heartbeat-{hb.get('status', '').lower()}",
                f"⚠️ Brain reports {hb.get('status')}: {hb.get('message')}",
            ))

    if state.get('brain_status') == 'TOKEN_EXPIRED':
        alerts.append((
            P1, 'token-expired',
            '🚨 enc_token EXPIRED mid-day — session ended. Paste a fresh '
            'token and restart from the dashboard.',
        ))

    # Zero trades by 11:00 with a session supposedly active
    trades_today = state.get('trades_today')
    if (
        m >= 11 * 60
        and state.get('active_session_id')
        and trades_today == 0
    ):
        alerts.append((
            P2, f"zero-trades-{day}",
            '⚠️ Session active but ZERO trades logged by 11:00 IST — '
            'check the dashboard (signals may all be HOLD, or something '
            'is silently wrong).',
        ))

    return alerts


def fetch_state(sb) -> dict:
    state = {'heartbeat': None, 'brain_status': None,
             'active_session_id': None, 'trades_today': None}
    try:
        res = sb.table('brain_heartbeat').select('*').eq('id', 1).limit(1).execute()
        state['heartbeat'] = res.data[0] if res.data else None
    except Exception as e:
        print(f"[watchdog] heartbeat fetch failed: {e}")

    try:
        res = (
            sb.table('app_config').select('key,value')
            .in_('key', ['brain_status', 'active_session_id',
                         'deploy_incident', 'token_incident'])
            .execute()
        )
        cfg = {r['key']: r['value'] for r in (res.data or [])}
        state['brain_status'] = cfg.get('brain_status')
        state['active_session_id'] = cfg.get('active_session_id') or None
        state['deploy_incident'] = cfg.get('deploy_incident') or None
        state['token_incident'] = cfg.get('token_incident') or None
    except Exception as e:
        print(f"[watchdog] config fetch failed: {e}")

    try:
        now_ist = datetime.now(IST)
        day_start_utc = IST.localize(
            datetime(now_ist.year, now_ist.month, now_ist.day)
        ).astimezone(timezone.utc)
        res = (
            sb.table('trades').select('id')
            .gte('created_at', day_start_utc.isoformat())
            .limit(1)
            .execute()
        )
        state['trades_today'] = 1 if res.data else 0
    except Exception as e:
        print(f"[watchdog] trades fetch failed: {e}")

    return state


def main():
    url = os.getenv('SUPABASE_URL')
    key = os.getenv('SUPABASE_SERVICE_KEY')
    if not url or not key:
        raise RuntimeError('SUPABASE_URL / SUPABASE_SERVICE_KEY required')
    sb = create_client(url, key)

    print(
        f"[watchdog] started {datetime.now(IST).strftime('%Y-%m-%d %H:%M:%S IST')} "
        f"telegram={'ON' if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID else 'OFF (print only)'}"
    )
    send_alert('watchdog-start', '👀 Watchdog online.', tier=P3)

    # Durable one-shot flags: consume once alerted so they don't re-fire
    # every dedup window. Maps a dedup-key prefix → app_config key to clear.
    one_shot = {'deploy-incident-': 'deploy_incident',
                'token-incident-': 'token_incident'}

    while True:
        try:
            now_ist = datetime.now(IST)
            state = fetch_state(sb)
            for tier, alert_key, message in evaluate(state, now_ist):
                sent = send_alert(alert_key, message, tier=tier)
                if not sent:
                    continue
                for prefix, cfg_key in one_shot.items():
                    if alert_key.startswith(prefix):
                        try:
                            sb.table('app_config').upsert(
                                {'key': cfg_key, 'value': ''}
                            ).execute()
                        except Exception as e:
                            print(f"[watchdog] failed to clear {cfg_key}: {e}")
        except Exception as e:
            print(f"[watchdog] loop error: {e}")
        time.sleep(CHECK_INTERVAL_SECONDS)


if __name__ == '__main__':
    main()
