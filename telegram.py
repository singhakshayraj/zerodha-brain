"""Shared Telegram send — one thin wrapper, many bots.

Extracted from watchdog.py so a second bot (portfolio-advisor digest/alerts)
can send without importing watchdog's Supabase-polling machinery. Policy —
dedup windows, alert tiers, retry — stays with each caller; this module only
delivers one message and never raises.
"""
import requests


def send_message(token: str, chat_id: str, text: str, timeout: int = 10,
                 reply_markup: dict = None) -> bool:
    """POST one message to a Telegram chat. Returns True on a 2xx send,
    False otherwise (missing creds, network error, non-2xx) — never raises,
    so callers can fire-and-forget from trading-adjacent loops.
    reply_markup: optional inline keyboard ({'inline_keyboard': [[...]]})."""
    if not token or not chat_id:
        return False
    try:
        payload = {'chat_id': chat_id, 'text': text}
        if reply_markup:
            payload['reply_markup'] = reply_markup
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json=payload,
            timeout=timeout,
        ).raise_for_status()
        return True
    except Exception as e:
        print(f"[telegram] send failed: {e}")
        return False


def get_updates(token: str, offset: int = None, timeout: int = 25) -> list:
    """Long-poll getUpdates — returns the raw update list ([] on any
    failure). Callers own offset bookkeeping (pass max(update_id)+1 back)."""
    if not token:
        return []
    try:
        params = {'timeout': timeout, 'allowed_updates': ['callback_query']}
        if offset is not None:
            params['offset'] = offset
        r = requests.get(
            f"https://api.telegram.org/bot{token}/getUpdates",
            json=params, timeout=timeout + 10)
        r.raise_for_status()
        return (r.json() or {}).get('result') or []
    except Exception as e:
        print(f"[telegram] get_updates failed: {e}")
        return []


def answer_callback(token: str, callback_query_id: str, text: str = '',
                    timeout: int = 10) -> bool:
    """Ack a button tap (stops the client-side spinner) with an optional
    toast. Never raises."""
    if not token or not callback_query_id:
        return False
    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/answerCallbackQuery",
            json={'callback_query_id': callback_query_id, 'text': text},
            timeout=timeout,
        ).raise_for_status()
        return True
    except Exception as e:
        print(f"[telegram] answer_callback failed: {e}")
        return False
