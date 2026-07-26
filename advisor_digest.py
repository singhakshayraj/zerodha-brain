"""Telegram digest for the portfolio advisor — text + inline Accept/Decline
keyboard + the once-a-day send. Extracted from portfolio_advisor (SE4 module
split) and re-exported there for backward compatibility.

ADVISORY ONLY: records decisions (advisor_bot verifies the callback); nothing
here places, modifies, or cancels an order.
"""
import config
import database as db
import telegram
from advisor_risk import build_portfolio_risk_lines

_ACTIONABLE = ('SELL', 'SELL_ON_BOUNCE', 'TRIM')

# Telegram rejects messages over 4096 chars — a heavy day (many actionable
# calls, each with a rotation + sizing block) can clear that. Cap the digest
# to the worst N; the rest are one tap away on /advisor.
_DIGEST_MAX_CALLS = 12


def build_digest(rows: list, run_date: str, risk: dict = None) -> str:
    """Telegram text for the day's ACTIONABLE calls only — HOLDs are noise in
    a push channel. Empty string = nothing worth sending today. `risk` is the
    optional portfolio_risk() read; its concentration + tax-loss lines append
    below the per-name calls."""
    act = [r for r in rows or []
           if r.get('verdict') in _ACTIONABLE or r.get('rotation_target_symbol')]
    risk_lines = build_portfolio_risk_lines(risk)
    if not act and not risk_lines:
        return ''
    if not act:
        # Nothing to trade, but a concentration/harvest flag is still worth
        # the one push.
        return '\n'.join([f"📋 Portfolio Advisor — {run_date}"] + risk_lines[1:])
    act.sort(key=lambda r: r.get('trend_score') or 0)
    overflow = len(act) - _DIGEST_MAX_CALLS
    act = act[:_DIGEST_MAX_CALLS]
    lines = [f"📋 Portfolio Advisor — {run_date}",
             f"{len(act) + max(0, overflow)} actionable of {len(rows)} holdings:"]
    for r in act:
        pnl = r.get('pnl_percent')
        line = (f"\n{r['symbol']}: {r['verdict']} "
                f"(trend {r.get('trend_score')}, "
                f"{'+' if (pnl or 0) >= 0 else ''}{pnl}%)")
        if r.get('exit_target'):
            line += f" — sell near ₹{r['exit_target']}"
        elif r.get('stop_level') and r.get('verdict') == 'TRIM':
            line += f" — keep rest only above ₹{r['stop_level']}"
        if r.get('rotation_target_symbol'):
            line += (f"\n  ↪ rotate into {r['rotation_target_symbol']} "
                     f"(score {r.get('rotation_target_score')}, "
                     f"{'same sector' if r.get('rotation_reason') == 'same_sector' else 'cross-sector'})")
            if r.get('rotation_buy_qty'):
                line += (f"\n  💰 sell {r.get('rotation_sell_qty')} → "
                         f"₹{r.get('rotation_freed_inr'):,.0f} frees "
                         f"~{r.get('rotation_buy_qty')} "
                         f"{r['rotation_target_symbol']} "
                         f"@ ₹{r.get('rotation_buy_price'):,.2f}")
        lines.append(line)
    if overflow > 0:
        lines.append(f"\n…and {overflow} more — full read on /advisor")
    lines.extend(risk_lines)
    lines.append("\nAdvisory only — you decide. Full read: /advisor")
    return '\n'.join(lines)


def build_decision_keyboard(rows: list, run_date: str) -> dict:
    """Inline Accept/Decline buttons, one row per actionable call. Callback
    data 'adv|<run_date>|<symbol>|<accept/decline>' — parsed and verified by
    advisor_bot (which records the DECISION only; nothing here or there can
    place an order). None when nothing is actionable."""
    act = [r for r in rows or []
           if r.get('verdict') in _ACTIONABLE or r.get('rotation_target_symbol')]
    if not act:
        return None
    act.sort(key=lambda r: r.get('trend_score') or 0)
    act = act[:_DIGEST_MAX_CALLS]     # keyboard mirrors the digest's cap
    keyboard = [[
        {'text': f"✅ {r['symbol']}",
         'callback_data': f"adv|{run_date}|{r['symbol']}|accept"},
        {'text': f"❌ {r['symbol']}",
         'callback_data': f"adv|{run_date}|{r['symbol']}|decline"},
    ] for r in act]
    return {'inline_keyboard': keyboard}


def send_daily_digest(rows: list, run_date: str, risk: dict = None) -> bool:
    """One push per day after the advisor run. Dedup is durable (app_config
    'advisor_digest_date') so a manual advisor_run_now re-run doesn't
    double-send. No-ops without the flag + both bot creds. Never raises.
    `risk` is the optional portfolio_risk() read appended to the digest."""
    try:
        if not (config.ADVISOR_DIGEST_ENABLED
                and config.ADVISOR_TELEGRAM_BOT_TOKEN
                and config.ADVISOR_TELEGRAM_CHAT_ID):
            return False
        if (db.get_config('advisor_digest_date') or '') == run_date:
            return False
        text = build_digest(rows, run_date, risk=risk)
        if not text:
            return False
        markup = (build_decision_keyboard(rows, run_date)
                  if config.ADVISOR_DECISIONS_ENABLED else None)
        if markup:
            text += ("\n\nTap ✅/❌ per call to record your decision — the "
                     "track record then judges accepted and declined calls "
                     "separately. (Recording only; no order is placed.)")
        sent = telegram.send_message(config.ADVISOR_TELEGRAM_BOT_TOKEN,
                                     config.ADVISOR_TELEGRAM_CHAT_ID, text,
                                     reply_markup=markup)
        if sent:
            db.write_config('advisor_digest_date', run_date)
        return sent
    except Exception as e:
        print(f"[advisor] digest failed (non-fatal): {e}")
        return False
