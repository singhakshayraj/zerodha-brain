"""[P-51] Real-money P&L reconciliation — bookkeeping, not a model score.

Daily mark-to-market P&L per account from Zerodha fills (`tradebook`) and
daily closes:

    pnl(D) = value_close(D) - value_open(D) - buys(D) + sells(D)
    value_open(D) = Σ qty_eod(D-1) × close(D-1)
    value_close(D) = Σ qty_eod(D) × close(D)

Exact for any mix of trades (same-day round trips included), needs no cost
basis, and days sum to the month. Positions per day are reconstructed from an
anchor holdings snapshot by walking fills forward/back from the anchor time.
Close = `candles` interval='day', falling back to the advisor's last observed
price that day (ETFs and non-Nifty-500 names have no stored bars).

Decision ledger: each fill is tagged *followed* when a matching call stood in
the prior FOLLOW_WINDOW trading days (SELL/SELL_ON_BOUNCE/TRIM for a sell; the
rotation target of a holding's call for a buy), else *own*; official SELL/TRIM
calls never acted on within the window are *ignored*. Impact is measured
IMPACT_DAYS trading days later (or the latest close if sooner):
  followed/own sell  : qty × (sell price − later close)    (+ = saved by selling)
  followed/own buy   : qty × (later close − buy price)     (+ = gained by buying)
  ignored call       : qty × (later close − price at call) (+ = holding paid)
Nothing here enters any trial or the n = 400 read.
"""
from collections import defaultdict
from datetime import datetime

import pytz

import database as db

IST = pytz.timezone('Asia/Kolkata')
START = '2026-08-26'          # first stored daily close
FOLLOW_WINDOW = 3             # trading days a call stays "standing"
IMPACT_DAYS = 10
SELL_CALLS = ('SELL', 'SELL_ON_BOUNCE', 'TRIM')
INDEX = 'NIFTY 50'
T1_IN_HOLDINGS_FROM = '2026-11-12'


# ── pure core ────────────────────────────────────────────────────────────────

def signed(f: dict) -> float:
    return float(f['quantity']) * (1 if f['trade_type'] == 'buy' else -1)


def positions_by_day(anchor: dict, anchor_time: str, fills: list, days: list,
                     unsettled_from: str = None) -> dict:
    """{day: {symbol: qty at end of day}}. anchor = holdings at anchor_time
    (ISO). Fills before the anchor are undone walking back; fills after are
    applied walking forward. Zerodha's holdings `quantity` excludes buys not
    yet settled (T+1), so a buy dated >= unsettled_from is treated as if it
    executed after the anchor even when it is earlier on the clock."""
    out = {}
    for d in days:
        pos = defaultdict(float, {s: float(q) for s, q in anchor.items()})
        eod = f"{d}T23:59:59"
        for f in fills:
            t = str(f['executed_at'])[:19]
            if (unsettled_from and f['trade_type'] == 'buy'
                    and str(f['trade_date']) >= unsettled_from and t <= anchor_time):
                t = anchor_time + 'Z'            # sorts just after the anchor
            if anchor_time < t <= eod:
                pos[f['symbol']] += signed(f)
            elif eod < t <= anchor_time:
                pos[f['symbol']] -= signed(f)
        out[d] = {s: q for s, q in pos.items() if abs(q) > 1e-9}
    return out


def daily_pnl(days: list, positions: dict, closes: dict, fills: list) -> list:
    """Pure. closes: {symbol: {day: close}}. Returns one row per day (from
    days[1:]); symbols without a close on either side are listed as missing
    and valued at their last known close."""
    by_day = defaultdict(list)
    for f in fills:
        by_day[str(f['trade_date'])].append(f)
    last = {}

    def px(sym, day):
        c = (closes.get(sym) or {}).get(day)
        if c is not None:
            last[sym] = c
            return c, False
        return last.get(sym), True

    rows = []
    for d in days[:1]:
        for s in positions.get(d, {}):
            px(s, d)
    for prev, d in zip(days, days[1:]):
        missing = set()
        v_open = 0.0
        for s, q in positions.get(prev, {}).items():
            p = last.get(s)
            if p is None:
                missing.add(s)
                continue
            v_open += q * p
        v_close = 0.0
        for s, q in positions.get(d, {}).items():
            p, miss = px(s, d)
            if p is None:
                missing.add(s)
                continue
            if miss:
                missing.add(s)
            v_close += q * p
        buys = sum(float(f['quantity']) * float(f['price']) for f in by_day[d] if f['trade_type'] == 'buy')
        sells = sum(float(f['quantity']) * float(f['price']) for f in by_day[d] if f['trade_type'] == 'sell')
        nifty = closes.get(INDEX) or {}
        n0, n1 = nifty.get(prev), nifty.get(d)
        rows.append({
            'date': d, 'pnl': round(v_close - v_open - buys + sells, 2),
            'value_open': round(v_open, 2), 'value_close': round(v_close, 2),
            'nifty_pnl': round(v_open * (n1 / n0 - 1), 2) if n0 and n1 else None,
            'buys_inr': round(buys, 2), 'sells_inr': round(sells, 2),
            'fills': len(by_day[d]), 'missing_prices': sorted(missing) or None,
        })
    return rows


def _later_close(closes: dict, sym: str, days: list, d: str, n: int):
    """(close, days_used, asof) n trading days after d, or the latest."""
    after = [x for x in days if x > d and (closes.get(sym) or {}).get(x) is not None]
    if not after:
        return None, 0, None
    k = min(n, len(after))
    return closes[sym][after[k - 1]], k, after[k - 1]


def ledger(fills: list, calls: list, closes: dict, days: list) -> list:
    """Pure. calls: official advice rows {symbol, run_date, verdict, quantity,
    last_price, rotation_target_symbol}. Returns decision_ledger rows."""
    def window(d):
        prior = [x for x in days if x <= d]
        return set(prior[-(FOLLOW_WINDOW + 1):])

    sell_calls = defaultdict(list)          # symbol -> calls
    buy_targets = defaultdict(list)         # target -> calls
    for c in calls:
        if c['verdict'] in SELL_CALLS:
            sell_calls[c['symbol']].append(c)
        if c.get('rotation_target_symbol'):
            buy_targets[c['rotation_target_symbol']].append(c)

    # One decision per (symbol, day, side): merge partial fills at VWAP.
    merged = {}
    for f in fills:
        k = (f['symbol'], str(f['trade_date']), f['trade_type'])
        m = merged.setdefault(k, {**f, 'quantity': 0.0, '_notional': 0.0})
        m['quantity'] += float(f['quantity'])
        m['_notional'] += float(f['quantity']) * float(f['price'])
    orders = [{**m, 'price': m['_notional'] / m['quantity']} for m in merged.values() if m['quantity']]

    out, acted = [], set()
    for f in orders:
        d, sym, q, p = str(f['trade_date']), f['symbol'], float(f['quantity']), float(f['price'])
        if d < START:
            continue
        w = window(d)
        if f['trade_type'] == 'sell':
            match = [c for c in sell_calls[sym] if str(c['run_date']) in w]
        else:
            match = [c for c in buy_targets[sym] if str(c['run_date']) in w]
        c = max(match, key=lambda c: str(c['run_date'])) if match else None
        if c and f['trade_type'] == 'sell':
            acted.add((sym, str(c['run_date'])))
        later, k, asof = _later_close(closes, sym, days, d, IMPACT_DAYS)
        impact = None
        if later is not None:
            impact = q * (p - later) if f['trade_type'] == 'sell' else q * (later - p)
        out.append({
            'kind': ('followed_' if c else 'own_') + f['trade_type'],
            'symbol': sym, 'date': d, 'side': f['trade_type'], 'qty': q, 'price': round(p, 4),
            'call_run_date': str(c['run_date']) if c else None,
            'call_verdict': c['verdict'] if c else None,
            'rotation_from': c['symbol'] if c and f['trade_type'] == 'buy' else None,
            'impact_inr': round(impact, 2) if impact is not None else None,
            'impact_days': k, 'impact_asof': asof,
        })
    # Ignored: an official sell call with no sell of that symbol inside its window.
    sold_days = defaultdict(set)
    for f in fills:
        if f['trade_type'] == 'sell':
            sold_days[f['symbol']].add(str(f['trade_date']))
    from bisect import bisect_right
    last_ignored = {}                        # symbol -> day index of its last ignored row
    for c in sorted(calls, key=lambda c: str(c['run_date'])):
        if c['verdict'] not in SELL_CALLS or str(c['run_date']) < START:
            continue
        rd, sym = str(c['run_date']), c['symbol']
        if (sym, rd) in acted:
            continue
        fwd = [x for x in days if x >= rd][:FOLLOW_WINDOW + 1]
        if sold_days[sym] & set(fwd):
            continue
        i = bisect_right(days, rd)
        if sym in last_ignored and i - last_ignored[sym] <= FOLLOW_WINDOW + 1:
            continue                         # repeated call inside one window: one row
        last_ignored[sym] = i
        qty = float(c.get('quantity') or 0) * (0.5 if c['verdict'] == 'TRIM' else 1)
        base = float(c.get('last_price') or 0)
        later, k, asof = _later_close(closes, sym, days, rd, IMPACT_DAYS)
        out.append({
            'kind': 'ignored_call', 'symbol': sym, 'date': rd, 'side': None,
            'qty': qty, 'price': base, 'call_run_date': rd, 'call_verdict': c['verdict'],
            'rotation_from': None,
            'impact_inr': round(qty * (later - base), 2) if later is not None and base else None,
            'impact_days': k, 'impact_asof': asof,
        })
    return out


# ── I/O ──────────────────────────────────────────────────────────────────────

def _ist(ts: str) -> str:
    """'2026-11-11T13:33:18.41192+00:00' -> IST 'YYYY-MM-DDTHH:MM:SS'."""
    base, tz = str(ts)[:19], str(ts)[19:]
    off = tz[tz.find('+'):] if '+' in tz else ('-' + tz.split('-')[-1] if '-' in tz else '+00:00')
    dt = datetime.fromisoformat(base + off.replace('Z', '+00:00'))
    return dt.astimezone(IST).strftime('%Y-%m-%dT%H:%M:%S')


def _paged(q_fn) -> list:
    out, off = [], 0
    while True:
        page = q_fn().range(off, off + 999).execute().data or []
        out += page
        if len(page) < 1000:
            return out
        off += 1000


def _load(account: str):
    sb = db.supabase
    fills = _paged(lambda: sb.table('tradebook').select('*').eq('account', account)
                   .order('executed_at'))
    if account == 'primary':
        last = (sb.table('portfolio_advice').select('run_id,created_at,run_date')
                .eq('is_official', True).order('created_at', desc=True).limit(1).execute().data)
        anchor_rows = (sb.table('portfolio_advice').select('symbol,quantity')
                       .eq('run_id', last[0]['run_id']).execute().data) if last else []
        anchor_time = _ist(last[0]['created_at']) if last else '1970-01-01'
        calls = _paged(lambda: sb.table('portfolio_advice')
                       .select('symbol,run_date,verdict,quantity,last_price,rotation_target_symbol')
                       .eq('is_official', True).gte('run_date', START).order('run_date'))
        obs = _paged(lambda: sb.table('portfolio_advice').select('symbol,run_date,created_at,last_price')
                     .gte('run_date', START).order('created_at'))
    else:
        last = (sb.table('account_calls').select('run_date,created_at').eq('account', account)
                .order('run_date', desc=True).limit(1).execute().data)
        anchor_rows = (sb.table('account_calls').select('symbol,quantity').eq('account', account)
                       .eq('run_date', last[0]['run_date']).execute().data) if last else []
        anchor_time = _ist(last[0]['created_at']) if last else '1970-01-01'
        calls = _paged(lambda: sb.table('account_calls')
                       .select('symbol,run_date,verdict,quantity,last_price,rotation_target_symbol')
                       .eq('account', account).order('run_date'))
        obs = [{'symbol': c['symbol'], 'run_date': c['run_date'], 'last_price': c['last_price']}
               for c in calls]
    anchor = {r['symbol']: r['quantity'] for r in anchor_rows if r.get('quantity')}
    for f in fills:                          # Console times are IST-naive; API ones may carry +05:30
        f['executed_at'] = str(f['executed_at']).replace(' ', 'T')[:19]
    syms = sorted({f['symbol'] for f in fills} | set(anchor) | {INDEX})
    closes = defaultdict(dict)
    for sym in syms:
        for b in _paged(lambda: sb.table('candles').select('trade_date,close')
                        .eq('interval', 'day').eq('symbol', sym).gte('trade_date', START)
                        .order('trade_date')):
            closes[sym][str(b['trade_date'])] = float(b['close'])
    have_bar = {(s, d) for s, m in closes.items() for d in m}
    for o in obs:                            # ordered by time: last price of the day wins
        s, d = o['symbol'], str(o['run_date'])
        if o.get('last_price') and (s, d) not in have_bar:
            closes[s][d] = float(o['last_price'])
    days = sorted(closes[INDEX])
    return fills, anchor, anchor_time, calls, dict(closes), days


def run(account: str = 'primary') -> dict:
    """Recompute pnl_daily + decision_ledger for one account. Returns summary."""
    fills, anchor, anchor_time, calls, closes, days = _load(account)
    if len(days) < 2:
        return {'account': account, 'days': 0}
    a_day = anchor_time[:10]
    prior = [d for d in days if d < a_day]
    # Holdings count T+1 buys from 2026-11-12 (kite_client.get_holdings), so
    # only the anchor day's own buys are missing; older anchors also missed
    # the previous day's.
    unsettled = a_day if a_day >= T1_IN_HOLDINGS_FROM else (prior[-1] if prior else a_day)
    pos = positions_by_day(anchor, anchor_time, fills, days, unsettled_from=unsettled)
    rows = daily_pnl(days, pos, closes, [f for f in fills if str(f['trade_date']) >= START])
    now = datetime.now(IST).isoformat()
    for r in rows:
        r.update(account=account, computed_at=now)
    for i in range(0, len(rows), 500):
        db.supabase.table('pnl_daily').upsert(rows[i:i + 500], on_conflict='account,date').execute()
    led = ledger(fills, calls, closes, days)
    db.supabase.table('decision_ledger').delete().eq('account', account).execute()
    for r in led:
        r.update(account=account, computed_at=now)
    for i in range(0, len(led), 500):
        db.supabase.table('decision_ledger').insert(led[i:i + 500]).execute()
    total = round(sum(r['pnl'] for r in rows), 2)
    print(f"[reconcile:{account}] {len(rows)} days {days[1]}→{days[-1]} · P&L ₹{total:,.0f} · "
          f"ledger {len(led)} rows · anchor {anchor_time} ({len(anchor)} names)")
    return {'account': account, 'days': len(rows), 'pnl': total, 'ledger': len(led)}
