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
from advisor_rotation import find_rotation_candidate, size_rotation
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
    # Sector layer on top of v1 (trial 261): v1 kept as v1_verdict, the
    # sector-aware call becomes this account's verdict. Before rotations so
    # sizing follows the shown call (TRIM = half).
    sectors = None
    try:
        import sector_tilt
        import sector_view
        sectors = sector_view.compute(today, rows)
        trends = {g['group']: g['trend_median'] for g in sectors['groups']
                  if g['names'] >= sector_tilt.MIN_NAMES and g['trend_median'] is not None}
        sector_tilt.apply(rows, trends, sector_map)
        for r in rows:
            r['v1_verdict'] = r['verdict']
            r['verdict'] = r['sector_verdict']
    except Exception as e:
        print(f"[preview:{acct}] sector layer skipped (non-fatal): {e}")
    try:
        attach_rotations(rows, today, regime, acct)
    except Exception as e:
        print(f"[preview:{acct}] rotation skipped (non-fatal): {e}")
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
            'holdings_count': len(rows), 'rows': rows, 'portfolio_risk': risk,
            'sector_view': sectors}


def todays_universe_scores(today: str, exclude: set) -> dict:
    """Rotation pool from the primary account's Nifty-500 scan stored in
    stock_universe — no second 500-name scan on this token. Empty when the
    scan hasn't run today."""
    out = {}
    for r in db.get_universe_by_sector(None, list(exclude)):
        if (r.get('advisor_score_updated_at') or '')[:10] != today:
            continue
        detail = r.get('advisor_detail') or {}
        out[r['symbol']] = {'symbol': r['symbol'], 'score': r['advisor_score'],
                            'sector': r.get('sector'),
                            'last_close': detail.get('last_price')}
    return out


def attach_rotations(rows: list, today: str, regime: str, account: str = 'natwar') -> int:
    """Same rotation call as the official run (gate + sizing), fed from today's
    stored scan. Returns targets attached."""
    scored = todays_universe_scores(today, {r['symbol'] for r in rows})
    if not scored:
        return 0
    sector_of = {u['symbol']: u.get('sector') for u in config.NIFTY500_UNIVERSE}
    min_gap = market_regime.rotation_min_gap_for(regime)
    try:                                     # household book (P-53)
        import household
        guard = household.guard_for(account, rows)
    except Exception as e:
        print(f"[preview] household unavailable (non-fatal): {e}")
        guard = None
    n = 0
    for row in rows:
        if guard is not None:
            buy = household.buy_estimate(row)
            t, note = household.pick(row.get('trend_score'), sector_of.get(row['symbol']),
                                     scored, guard, buy, min_gap=min_gap, sold_sym=row['symbol'])
            if note:
                row.setdefault('indicators', {})['household'] = note
        else:
            t = find_rotation_candidate(row.get('trend_score'), sector_of.get(row['symbol']),
                                        scored, min_gap=min_gap)
        if not t:
            continue
        row['rotation_target_symbol'] = t['symbol']
        row['rotation_target_score'] = t['score']
        row['rotation_reason'] = t['reason']
        sizing = size_rotation(row.get('verdict'), row.get('quantity'),
                               row.get('last_price'), t.get('last_close'))
        row.update(sizing)
        if guard is not None and sizing.get('rotation_buy_qty'):
            guard.commit(row['symbol'], sizing.get('rotation_freed_inr') or 0, t['symbol'],
                         sizing['rotation_buy_qty'] * (sizing.get('rotation_buy_price') or 0))
        size_note = ''
        if sizing.get('rotation_buy_qty'):
            size_note = (f" Size: sell {sizing['rotation_sell_qty']} "
                         f"(₹{sizing['rotation_freed_inr']:,.0f}) → buy ~{sizing['rotation_buy_qty']} "
                         f"@ ₹{sizing['rotation_buy_price']:,.2f}")
        row['reasons'] = (row.get('reasons') or []) + [
            f"Rotation: {t['symbol']} scores {t['score']} vs this name's "
            f"{row.get('trend_score')} — freed capital has a stronger home.{size_note}"]
        n += 1
    return n


CALLS_TABLE = 'account_calls'


def call_rows(payload: dict) -> list:
    """Pure: one graded-track row per advised holding (INSUFFICIENT kept so
    the grader can clear it, like the holdings queue)."""
    out = []
    for r in payload.get('rows') or []:
        th = (r.get('indicators') or {}).get('thesis') or {}
        out.append({
            'account': payload['account'], 'run_date': payload['run_date'],
            'symbol': r['symbol'], 'verdict': r.get('v1_verdict') or r.get('verdict'),
            'sector_verdict': r.get('sector_verdict'), 'sector_trend': r.get('sector_trend'),
            'sector_rule': r.get('sector_rule'),
            'trigger_type': r.get('trigger_type'), 'trend_score': r.get('trend_score'),
            'confidence': r.get('confidence'), 'regime': payload.get('regime'),
            'last_price': r.get('last_price'), 'quantity': r.get('quantity'),
            'thesis_action': th.get('action'), 'quality': th.get('quality'),
            'rotation_target_symbol': r.get('rotation_target_symbol'),
        })
    return [x for x in out if x['verdict']]


def log_calls(payload: dict, overwrite: bool = False) -> int:
    """First preview of the day is the call: later same-day runs (a re-pasted
    token) never overwrite it — except the rerun after the primary scan
    lands, which replaces calls made without rotation targets."""
    rows = call_rows(payload)
    if rows:
        db.supabase.table(CALLS_TABLE).upsert(
            rows, on_conflict='account,run_date,symbol',
            ignore_duplicates=not overwrite).execute()
    return len(rows)


def run_preview(acct: str, market_data, overwrite: bool = False) -> int:
    """Build and store the preview. Returns rows stored."""
    payload = build_preview(market_data, acct)
    db.write_config(result_key(acct), json.dumps(payload, default=str))
    try:
        import universe_calls
        n_log = log_calls(payload, overwrite)
        n_grd = universe_calls.grade_due(payload['run_date'], CALLS_TABLE,
                                         'account,run_date,symbol')
        print(f"[preview:{acct}] calls logged {n_log}, graded {n_grd}")
    except Exception as e:
        print(f"[preview:{acct}] call log/grade failed (non-fatal): {e}")
    print(f"[preview:{acct}] stored {payload['holdings_count']} holdings "
          f"(regime {payload['regime']})")
    return payload['holdings_count']
