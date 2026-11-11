"""Second-account advisor PREVIEW (P-45 provisional, 2026-11-11).

Runs the same per-holding advisor (advise + thesis engine + portfolio risk)
on another Kite account's holdings, using that account's own token
(app_config 'enc_token_<acct>', pasted on /connect). The result goes to ONE
app_config JSON blob ('advisor_<acct>_latest') — never to portfolio_advice,
so the primary account's track record, grading, paper books and the V-20
n=400 read are untouched by construction. Not graded; no digest; no orders.
The full multi-account design (P-45 spec) replaces this after V-20.
"""
import json
from datetime import datetime

import pytz

import config
import database as db
import fundamentals
import market_regime
import thesis
from advisor_risk import portfolio_risk
from advisor_scoring import NIFTY50_INDEX_TOKEN, advise, completed_bars, news_sentiment

IST = pytz.timezone('Asia/Kolkata')
PREVIEW_ACCOUNTS = {'natwar': 'Natwar Raj Singh'}


def token_key(acct: str) -> str:
    return f'enc_token_{acct}'


def result_key(acct: str) -> str:
    return f'advisor_{acct}_latest'


def build_preview(market_data, acct: str, today: str = None) -> dict:
    """Pure-ish: reads holdings + candles via market_data, returns the payload.
    Per-symbol failures skip that symbol."""
    now = datetime.now(IST)
    today = today or now.date().isoformat()
    holdings = market_data.kite.get_holdings() or []
    for h in holdings:
        if h.get('tradingsymbol') and h.get('instrument_token'):
            market_data._instrument_cache[
                f"{h.get('exchange') or 'NSE'}:{h['tradingsymbol']}"] = h['instrument_token']

    nifty_candles = []
    try:
        market_data._instrument_cache['NSE:NIFTY 50'] = NIFTY50_INDEX_TOKEN
        nifty_candles = completed_bars(market_data.get_candles('NSE:NIFTY 50', 'day', 400) or [])
    except Exception as e:
        print(f"[preview:{acct}] nifty unavailable (non-fatal): {e}")
    nifty_closes = [float(c['close']) for c in nifty_candles if c.get('close') is not None]
    regime = market_regime.get_market_regime(nifty_candles)['regime']

    total = sum((h.get('quantity') or 0) * (h.get('last_price') or 0) for h in holdings)
    rows, closes_by_symbol = [], {}
    for h in holdings:
        sym, qty = h.get('tradingsymbol'), h.get('quantity') or 0
        if not sym or qty <= 0:
            continue
        try:
            candles = market_data.get_candles(f"{h.get('exchange') or 'NSE'}:{sym}", 'day', 400) or []
            closes_by_symbol[sym] = [float(c['close']) for c in candles if c.get('close') is not None]
            weight = round(qty * (h.get('last_price') or 0) / total * 100, 1) if total else None
            try:
                sent = news_sentiment(sym)
            except Exception:
                sent = None
            adv = advise({'symbol': sym, 'quantity': qty,
                          'average_price': h.get('average_price'),
                          'last_price': h.get('last_price')},
                         candles, nifty_closes=nifty_closes,
                         portfolio_weight_pct=weight, news_sent=sent, regime=regime)
            rows.append({'run_date': today, **adv})
        except Exception as e:
            print(f"[preview:{acct}] {sym} skipped: {e}")

    sector_map = {u['symbol']: u.get('sector') for u in config.NIFTY500_UNIVERSE}
    try:
        thesis.attach_theses(rows, fundamentals.load_store(), sector_map, today)
    except Exception as e:
        print(f"[preview:{acct}] thesis skipped (non-fatal): {e}")
    risk = None
    try:
        risk = portfolio_risk(rows, sector_map=sector_map, closes_by_symbol=closes_by_symbol)
    except Exception as e:
        print(f"[preview:{acct}] risk skipped (non-fatal): {e}")
    return {'account': acct, 'label': PREVIEW_ACCOUNTS.get(acct, acct),
            'run_date': today, 'run_at': now.isoformat(), 'regime': regime,
            'holdings_count': len(rows), 'rows': rows, 'portfolio_risk': risk}


def run_preview(acct: str, market_data) -> int:
    """Build and store the preview. Returns rows stored."""
    payload = build_preview(market_data, acct)
    db.write_config(result_key(acct), json.dumps(payload, default=str))
    print(f"[preview:{acct}] stored {payload['holdings_count']} holdings "
          f"(regime {payload['regime']})")
    return payload['holdings_count']
