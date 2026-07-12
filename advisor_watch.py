"""Intraday holdings watch — Telegram push when a REAL holding moves hard.

A daemon thread (modeled on scheduler._heartbeat_thread) polls once per
ADVISOR_WATCH_INTERVAL_SECONDS during market hours. Each tick is ONE
GET /portfolio/holdings call — the payload already carries every holding's
LTP and previous close, so watching N holdings costs O(1) Kite requests.

Move basis: % vs previous close (the same day-change number the Kite app
shows). One alert per symbol+direction per day, in-memory dedup.

ADVISORY ONLY: reads holdings, sends a message. No order path. No-ops
entirely unless ADVISOR_INTRADAY_ALERTS_ENABLED + the advisor bot creds are
set, and never in QA.
"""
import threading
import time
from datetime import datetime

import pytz

import config
import database as db
import telegram
from kite_client import KiteClient

IST = pytz.timezone('Asia/Kolkata')

# (symbol, date, direction) → alerted. In-memory: a process restart may
# repeat one alert, which is the safe direction.
_alerted = set()


def _in_market_hours(now_ist: datetime) -> bool:
    if now_ist.weekday() > 4:
        return False
    if now_ist.strftime('%Y-%m-%d') in config.NSE_HOLIDAYS:
        return False
    m = now_ist.hour * 60 + now_ist.minute
    return (9 * 60 + 15) <= m <= (15 * 60 + 30)


def day_move_pct(holding: dict):
    """% move vs previous close. Prefers Kite's own day_change_percentage;
    falls back to computing from close_price. None when unknowable."""
    pct = holding.get('day_change_percentage')
    if pct is not None:
        return float(pct)
    last = holding.get('last_price')
    prev = holding.get('close_price')
    if not last or not prev:
        return None
    return (float(last) - float(prev)) / float(prev) * 100


def check_holdings(holdings: list, threshold_pct: float = None,
                   today: str = None) -> list:
    """Pure: holdings snapshot → alert texts for fresh threshold breaches.
    Marks each (symbol, date, direction) so a move alerts once per day."""
    threshold = (config.ADVISOR_INTRADAY_THRESHOLD_PCT
                 if threshold_pct is None else threshold_pct)
    today = today or datetime.now(IST).date().isoformat()
    out = []
    for h in holdings or []:
        try:
            sym = h.get('tradingsymbol')
            if not sym or (h.get('quantity') or 0) <= 0:
                continue
            pct = day_move_pct(h)
            if pct is None or abs(pct) < threshold:
                continue
            direction = 'up' if pct > 0 else 'down'
            key = (sym, today, direction)
            if key in _alerted:
                continue
            _alerted.add(key)
            arrow = '📈' if pct > 0 else '📉'
            qty = h.get('quantity') or 0
            last = float(h.get('last_price') or 0)
            avg = float(h.get('average_price') or 0)
            pos_pnl = ((last / avg - 1) * 100) if avg and last else None
            line = (f"{arrow} {sym} {'+' if pct > 0 else ''}{pct:.1f}% today "
                    f"(₹{last:.2f}, {qty} held")
            if pos_pnl is not None:
                line += f", position {'+' if pos_pnl >= 0 else ''}{pos_pnl:.1f}%"
            line += ")"
            if pct <= -config.ADVISOR_INTRADAY_THRESHOLD_PCT:
                line += "\nCheck /advisor before reacting — don't sell a panic low blind."
            else:
                line += "\nIf this name is on a TRIM/SELL verdict, strength like this is the exit window."
            out.append(line)
        except Exception as e:
            print(f"[advisor_watch] holding skipped: {e}")
    return out


def _watch_loop() -> None:
    while True:
        try:
            now = datetime.now(IST)
            if _in_market_hours(now):
                token = db.get_enc_token()
                if token:
                    holdings = KiteClient(token).get_holdings()
                    for text in check_holdings(holdings):
                        telegram.send_message(
                            config.ADVISOR_TELEGRAM_BOT_TOKEN,
                            config.ADVISOR_TELEGRAM_CHAT_ID, text)
        except Exception as e:
            print(f"[advisor_watch] tick error (loop continues): {e}")
        time.sleep(config.ADVISOR_WATCH_INTERVAL_SECONDS)


def start_advisor_watch() -> bool:
    """Spawn the watch thread once at scheduler startup. Refuses (False)
    when disabled, unkeyed, or in QA — safe to call unconditionally."""
    if not config.ADVISOR_INTRADAY_ALERTS_ENABLED or config.QA_MODE:
        return False
    if not (config.ADVISOR_TELEGRAM_BOT_TOKEN
            and config.ADVISOR_TELEGRAM_CHAT_ID):
        print("[advisor_watch] enabled but bot creds missing — not starting")
        return False
    t = threading.Thread(target=_watch_loop, daemon=True, name='advisor_watch')
    t.start()
    print(f"[advisor_watch] started — ±{config.ADVISOR_INTRADAY_THRESHOLD_PCT}% "
          f"day move, every {config.ADVISOR_WATCH_INTERVAL_SECONDS}s")
    return True
