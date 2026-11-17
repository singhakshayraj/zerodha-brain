"""Round-trip trading cost and after-tax proceeds for a listed-equity sale
[P-68 phase 0]. Pure: no I/O, no orders, no verdict reads this yet (Phase 1).

Tax constants are the post-July-2024 rates as reported for FY 2026-27; verify
against the Finance Act before Phase 1 lets a verdict act on them.
"""
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


def tax_lots(trades: list, symbol: str, account: str) -> list:
    """FIFO open lots [{qty, price, date}] for one symbol in one account."""
    rows = sorted((t for t in trades
                   if t.get('symbol') == symbol and t.get('account') == account),
                  key=lambda t: str(t.get('trade_date')))
    lots = []
    for t in rows:
        q = int(float(t.get('quantity') or 0))
        if str(t.get('trade_type', '')).lower() == 'buy':
            lots.append({'qty': q, 'price': float(t['price']),
                         'date': str(t['trade_date'])[:10]})
            continue
        while q > 0 and lots:                      # sell consumes oldest first
            take = min(q, lots[0]['qty'])
            lots[0]['qty'] -= take
            q -= take
            if lots[0]['qty'] == 0:
                lots.pop(0)
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
