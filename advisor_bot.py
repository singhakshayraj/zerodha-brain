"""Advisor decision bot — records Accept/Decline taps from the daily digest.

A daemon thread long-polls Telegram getUpdates (one request per
ADVISOR_BOT_POLL_SECONDS, ~free) and, for each button tap on a digest
message, writes the decision onto that advice row (user_decision +
decided_at). The track record then judges accepted and declined calls
separately — the honest read of whether following the advisor beats
ignoring it.

DECISIONS ONLY. This module records a choice and acks the tap. It imports
no Kite client and has no order path — accepting a SELL here changes one
text column, nothing else. Real execution, if ever built, is a separate
module behind its own flag and gates.

Security: taps are honored only from ADVISOR_TELEGRAM_CHAT_ID. Anyone else
who finds the bot gets ignored (and logged).
"""
import threading
import time
from datetime import datetime

import pytz

import config
import database as db
import telegram

IST = pytz.timezone('Asia/Kolkata')

# getUpdates cursor (max update_id + 1). Durable in app_config so a Railway
# redeploy can't replay already-processed taps (Telegram redelivers
# unconfirmed updates for a while — an in-memory cursor made that window
# a decision-reverting hazard).
_OFFSET_KEY = 'advisor_bot_offset'
_offset = None


def _load_offset():
    global _offset
    try:
        raw = db.get_config(_OFFSET_KEY)
        _offset = int(raw) if raw else None
    except Exception:
        _offset = None


def _save_offset():
    try:
        if _offset is not None:
            db.write_config(_OFFSET_KEY, str(_offset))
    except Exception as e:
        print(f"[advisor_bot] offset save failed (non-fatal): {e}")


def parse_callback(data: str):
    """'adv|2026-07-13|NTPC|accept' -> (run_date, symbol, decision).
    None for anything malformed or not ours."""
    try:
        tag, run_date, symbol, decision = (data or '').split('|')
        if tag != 'adv' or decision not in ('accept', 'decline') \
                or not run_date or not symbol:
            return None
        return run_date, symbol, decision
    except Exception:
        return None


def handle_update(update: dict) -> bool:
    """One getUpdates entry -> at most one recorded decision. Returns True
    when a decision was stored. Wrong-chat taps are ignored loudly."""
    cq = (update or {}).get('callback_query')
    if not cq:
        return False
    cq_id = cq.get('id')
    chat_id = str(((cq.get('message') or {}).get('chat') or {}).get('id') or '')
    if chat_id != str(config.ADVISOR_TELEGRAM_CHAT_ID):
        print(f"[advisor_bot] tap from foreign chat {chat_id} IGNORED")
        telegram.answer_callback(config.ADVISOR_TELEGRAM_BOT_TOKEN, cq_id,
                                 'Not authorized.')
        return False
    parsed = parse_callback(cq.get('data'))
    if not parsed:
        telegram.answer_callback(config.ADVISOR_TELEGRAM_BOT_TOKEN, cq_id)
        return False
    run_date, symbol, decision = parsed
    ok = db.record_advice_decision(run_date, symbol, decision)
    verb = 'ACCEPTED' if decision == 'accept' else 'DECLINED'
    telegram.answer_callback(
        config.ADVISOR_TELEGRAM_BOT_TOKEN, cq_id,
        f"{verb}: {symbol} ({run_date}) recorded — no order placed."
        if ok else f"Not recorded: {symbol} ({run_date}) — this call was "
                   f"already judged by the backtest, or the row is gone.")
    if ok:
        print(f"[advisor_bot] {run_date} {symbol}: {decision}")
    return ok


def _poll_once() -> int:
    """One long-poll cycle. Returns decisions recorded."""
    global _offset
    updates = telegram.get_updates(config.ADVISOR_TELEGRAM_BOT_TOKEN,
                                   offset=_offset,
                                   timeout=config.ADVISOR_BOT_POLL_SECONDS)
    n = 0
    for u in updates:
        try:
            if u.get('update_id') is not None:
                _offset = max(_offset or 0, u['update_id'] + 1)
            if handle_update(u):
                n += 1
        except Exception as e:
            print(f"[advisor_bot] update skipped: {e}")
    if updates:
        _save_offset()
    return n


def _bot_loop() -> None:
    while True:
        try:
            t0 = time.monotonic()
            _poll_once()
            # A healthy empty long-poll is held open ~ADVISOR_BOT_POLL_SECONDS
            # by Telegram's server. An instant return means the request
            # failed (get_updates swallows network errors and returns []) —
            # sleep the interval instead of spinning hot against a dead
            # network and hammering the API.
            if time.monotonic() - t0 < 5:
                time.sleep(config.ADVISOR_BOT_POLL_SECONDS)
        except Exception as e:
            print(f"[advisor_bot] poll error (loop continues): {e}")
            time.sleep(config.ADVISOR_BOT_POLL_SECONDS)


def start_advisor_bot() -> bool:
    """Spawn the poller once at scheduler startup. Refuses (False) when the
    feature is off, creds are missing, or in QA — safe to call blindly."""
    if not config.ADVISOR_DECISIONS_ENABLED or config.QA_MODE:
        return False
    if not (config.ADVISOR_TELEGRAM_BOT_TOKEN
            and config.ADVISOR_TELEGRAM_CHAT_ID):
        print("[advisor_bot] enabled but bot creds missing — not starting")
        return False
    _load_offset()
    t = threading.Thread(target=_bot_loop, daemon=True, name='advisor_bot')
    t.start()
    print(f"[advisor_bot] started — decision recording via "
          f"{config.ADVISOR_BOT_POLL_SECONDS}s long-poll (NO order path)")
    return True
