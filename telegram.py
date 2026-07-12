"""Shared Telegram send — one thin wrapper, many bots.

Extracted from watchdog.py so a second bot (portfolio-advisor digest/alerts)
can send without importing watchdog's Supabase-polling machinery. Policy —
dedup windows, alert tiers, retry — stays with each caller; this module only
delivers one message and never raises.
"""
import requests


def send_message(token: str, chat_id: str, text: str, timeout: int = 10) -> bool:
    """POST one message to a Telegram chat. Returns True on a 2xx send,
    False otherwise (missing creds, network error, non-2xx) — never raises,
    so callers can fire-and-forget from trading-adjacent loops."""
    if not token or not chat_id:
        return False
    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={'chat_id': chat_id, 'text': text},
            timeout=timeout,
        ).raise_for_status()
        return True
    except Exception as e:
        print(f"[telegram] send failed: {e}")
        return False
