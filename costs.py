"""Round-trip trading cost and after-tax proceeds for a listed-equity sale
[P-68 phase 0]. Pure: no I/O, no orders, no verdict reads this yet (Phase 1).

Tax constants are the post-July-2024 rates as reported for FY 2026-27; verify
against the Finance Act before Phase 1 lets a verdict act on them.
"""
import csv
import os
from datetime import date, timedelta
from dateutil.relativedelta import relativedelta

STCG_PCT = 20.0                 # listed equity held < 12 months (s.111A)
LTCG_PCT = 12.5                 # held >= 12 months (s.112A), above the exemption
LTCG_EXEMPT_INR = 125000.0      # per FY per PAN
STT_PCT = 0.1                   # delivery, each side
BROKERAGE_PCT = 0.0             # Zerodha CNC
# (min ADV in INR, impact % per side) — first bucket the ADV clears wins
IMPACT_BUCKETS = ((50e7, 0.05), (5e7, 0.15), (0, 0.40))


def impact_pct(adv_inr):
    """Per-side impact cost by average daily value traded; unknown -> worst."""
    if adv_inr is None:
        return IMPACT_BUCKETS[-1][1]
    for floor, pct in IMPACT_BUCKETS:
        if adv_inr >= floor:
            return pct
    return IMPACT_BUCKETS[-1][1]


def round_trip_cost_pct(adv_inr=None) -> float:
    return round(2 * (STT_PCT + BROKERAGE_PCT + impact_pct(adv_inr)), 4)


def _d(s):
    return date.fromisoformat(str(s)[:10])


LOT_EVENTS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data', 'lot_events.csv')


def load_lot_events(path: str = LOT_EVENTS_PATH) -> list:
    """Bonus / demerger events the tradebook never shows. `date` is the record
    date: trades dated before it are entitled (T+1 settlement)."""
    if not os.path.exists(path):
        return []
    with open(path) as fh:
        return [{**r, 'ratio': float(r['ratio']), 'cost_share': float(r['cost_share'] or 0)}
                for r in csv.DictReader(fh)]


def tax_lots(trades: list, symbol: str, account: str, events: list = None) -> list:
    """FIFO open lots [{qty, price, date}] for one symbol in one account.
    Follows renames (ISIN, or a `rename` event when the row has none), nets same-day round trips
    (intraday) before FIFO, and applies bonus/demerger `events`."""
    events = load_lot_events() if events is None else events
    names = {symbol} | {e['parent'] for e in events if e['kind'] == 'rename' and e['symbol'] == symbol}
    isins = {t.get('isin') for t in trades if t.get('symbol') in names and t.get('isin')}
    rows = [t for t in trades if t.get('account') == account
            and (t.get('symbol') in names or (t.get('isin') and t.get('isin') in isins))]
    by_day = {}
    for t in rows:
        by_day.setdefault(str(t.get('trade_date'))[:10], []).append(t)
    evs = sorted((e for e in events if e['kind'] != 'rename' and symbol in (e['symbol'], e.get('parent'))),
                 key=lambda e: e['date'])
    lots = []

    def apply_events(before: str):
        while evs and evs[0]['date'] <= before:
            e = evs.pop(0)
            if e['kind'] == 'bonus' and e['symbol'] == symbol:
                n = int(sum(l['qty'] for l in lots) * e['ratio'])
                if n:
                    lots.append({'qty': n, 'price': 0.0, 'date': e['lot_date']})
            elif e['kind'] == 'demerger' and e.get('parent') == symbol:
                for l in lots:
                    l['price'] *= 1 - e['cost_share']
            elif e['kind'] == 'demerger':
                par = tax_lots([t for t in trades if str(t.get('trade_date'))[:10] < e['date']],
                               e['parent'], account, events=[])
                cum = 0
                for l in par:          # floor on the running total: fractions were paid in cash
                    q = int((cum + l['qty']) * e['ratio'] + 1e-9) - int(cum * e['ratio'] + 1e-9)
                    cum += l['qty']
                    if q:
                        lots.append({'qty': q, 'price': l['price'] * e['cost_share'] / e['ratio'],
                                     'date': l['date']})

    for day in sorted(by_day):
        apply_events(day)
        buys = [t for t in by_day[day] if str(t.get('trade_type', '')).lower() == 'buy']
        sold = sum(int(float(t.get('quantity') or 0)) for t in by_day[day]) \
            - sum(int(float(t.get('quantity') or 0)) for t in buys)
        for t in buys:                              # same-day sells cancel same-day buys first
            q = int(float(t.get('quantity') or 0))
            take = min(q, sold)
            sold -= take
            if q - take:
                lots.append({'qty': q - take, 'price': float(t['price']), 'date': day})
        while sold > 0 and lots:                    # the rest consumes oldest first
            take = min(sold, lots[0]['qty'])
            lots[0]['qty'] -= take
            sold -= take
            if lots[0]['qty'] == 0:
                lots.pop(0)
    apply_events('9999-12-31')
    return lots


def after_tax_pct(lots: list, qty: int, sale_price: float, sale_date: str,
                  ltcg_used_fy: float = 0.0, adv_inr=None) -> dict:
    """Sell `qty` from FIFO `lots` at `sale_price` on `sale_date`.
    Returns gross/net % on the cost of the shares sold, the STCG/LTCG split,
    tax, trading cost, and the day the next still-short-term lot turns long."""
    sd = _d(sale_date)
    left, cost, stcg, ltcg, ltcg_date = int(qty), 0.0, 0.0, 0.0, None
    for lot in lots:
        if left <= 0:
            break
        take = min(left, lot['qty'])
        gain = take * (sale_price - lot['price'])
        cost += take * lot['price']
        # s.2(42A): "not more than twelve months" is short-term, so the day
        # exactly one year on is still STCG; long-term starts the day after.
        long_on = _d(lot['date']) + relativedelta(years=1) + timedelta(days=1)
        if sd >= long_on:
            ltcg += gain
        else:
            stcg += gain
            if ltcg_date is None or long_on.isoformat() < ltcg_date:
                ltcg_date = long_on.isoformat()
        left -= take
    matched = int(qty) - left                  # shares with a known lot; the rest have no buy date
    taxable_ltcg = max(0.0, ltcg - max(0.0, LTCG_EXEMPT_INR - ltcg_used_fy))
    # ponytail: a short-term loss is not netted against LTCG (s.70 allows it); Phase 1 decides
    tax = max(0.0, stcg) * STCG_PCT / 100 + taxable_ltcg * LTCG_PCT / 100
    proceeds = matched * sale_price
    trading_cost = round(proceeds * round_trip_cost_pct(adv_inr) / 100, 2)
    gross = proceeds - cost
    return {
        'gross_pct': round(gross / cost * 100, 2) if cost else None,
        'stcg_inr': round(stcg, 2), 'ltcg_inr': round(ltcg, 2),
        'tax_inr': round(tax, 2), 'cost_inr': trading_cost,
        'net_pct': round((gross - tax - trading_cost) / cost * 100, 2) if cost else None,
        'ltcg_date': ltcg_date, 'matched_qty': matched, 'unmatched_qty': left,
    }
