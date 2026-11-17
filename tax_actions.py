"""Tax-aware action for SELL-grade verdicts [P-68 phase 1]. Pure: no I/O.

Marginal FY tax (losses first) + a wait-for-LTCG rule priced against the
empirical drift of SELL-grade calls. Brought-forward losses from earlier FYs
are not counted (needs the ITR, not the broker statement).
"""
from costs import (STCG_PCT, LTCG_PCT, LTCG_EXEMPT_INR, after_tax_pct,
                   round_trip_cost_pct)

WAIT_CAP_DAYS = 60
CURVE_MIN_N = 20
SELL_GRADES = ('SELL', 'SELL_ON_BOUNCE', 'TRIM')
_CPS = (5, 10, 20, 40, 60)


def _tax(stcg, ltcg, exempt_left):
    if stcg < 0:                      # short-term loss offsets both buckets
        ltcg, stcg = ltcg + stcg, 0.0
    ltcg = max(0.0, ltcg)             # a long-term loss never offsets STCG
    return stcg * STCG_PCT / 100 + max(0.0, ltcg - exempt_left) * LTCG_PCT / 100


def fy_tax(stcg, ltcg, ctx) -> float:
    """Marginal tax of this sale = FY tax with it minus FY tax without it."""
    ytd = float(ctx.get('ytd_net_realised_inr') or 0.0)
    ex = max(0.0, LTCG_EXEMPT_INR - float(ctx.get('ltcg_used_fy') or 0.0))
    # ponytail: YTD net has no bucket split, treated as short-term (offsets both)
    return max(0.0, round(_tax(ytd + stcg, ltcg, ex) - _tax(ytd, 0.0, ex), 2))


def expected_drop_pct(curve, days):
    """Mean drop (positive %) of SELL-grade calls after `days`, or None if thin."""
    def at(cp):
        c = (curve or {}).get(str(cp))
        return c if c and (c.get('n') or 0) >= CURVE_MIN_N else None
    lo = max([c for c in _CPS if c <= days] or [5])
    a = at(lo)
    if a is None:
        return None
    mean = a['mean_ret_pct']
    if days > lo and lo != _CPS[-1]:
        hi = _CPS[_CPS.index(lo) + 1]
        b = at(hi)
        if b is not None:
            mean += (b['mean_ret_pct'] - mean) * (days - lo) / (hi - lo)
    return max(0.0, -mean)


def _sale(lots, qty, price, on, row, ctx, adv_inr):
    """STCG/LTCG split, marginal tax, trading cost, ltcg_date, matched/unmatched."""
    known = min(qty, sum(l['qty'] for l in lots))
    a = after_tax_pct(lots, known, price, on, ctx.get('ltcg_used_fy') or 0.0, adv_inr)
    un = qty - known                   # beyond known lots: synthetic short-term lot
    stcg = a['stcg_inr'] + un * (price - float(row.get('average_price') or price))
    cost = round(qty * price * round_trip_cost_pct(adv_inr) / 100, 2)
    return {'tax': fy_tax(stcg, a['ltcg_inr'], ctx), 'cost': cost,
            'ltcg_date': a['ltcg_date'], 'unmatched': un}


def _sell_qty(row):
    q = int(row['quantity'])
    if row.get('rotation_sell_qty'):
        return min(q, int(row['rotation_sell_qty']))
    return max(1, q // 2) if row['verdict'] == 'TRIM' else q


def _inr(x):
    return '₹{:,}'.format(int(round(x)))


def plan(row, lots, ctx, curve, today, adv_inr, trading_days):
    q = int(row.get('quantity') or 0)
    if q == 0:
        return None
    price = float(row['last_price'])
    out = {'action': None, 'ltcg_date': None, 'wait_days': None, 'tax_now_inr': None,
           'tax_after_inr': None, 'saving_inr': None, 'wait_cost_inr': None,
           'cost_inr': None, 'lots': None, 'unmatched_qty': 0, 'harvest_inr': 0,
           'harvest_note': None, 'text': None}
    known_total = sum(l['qty'] for l in lots)
    if row['verdict'] not in SELL_GRADES:       # HOLD etc: LTCG date only
        n = min(q, known_total)
        out['ltcg_date'] = after_tax_pct(lots, n, price, today)['ltcg_date']
        out['unmatched_qty'] = q - n
        out['lots'] = _lots_state(n, q)
        return out
    sq = _sell_qty(row)
    now = _sale(lots, sq, price, today, row, ctx, adv_inr)
    ld = now['ltcg_date'] or None
    known_sq = sq - now['unmatched']
    out.update(tax_now_inr=now['tax'], cost_inr=now['cost'], ltcg_date=ld,
               unmatched_qty=now['unmatched'], lots=_lots_state(known_sq, sq),
               action='sell now')
    value = sq * price
    note = ''
    if ld:
        wd = trading_days(today, ld)
        out['wait_days'] = wd
        if wd <= WAIT_CAP_DAYS:
            drop = expected_drop_pct(curve, wd)
            if drop is None:
                note = ' · not enough history to price a wait'
            else:
                after = _sale(lots, sq, price, ld, row, ctx, adv_inr)['tax']
                saving, cost_w = round(now['tax'] - after, 2), round(value * drop / 100, 2)
                out.update(tax_after_inr=after, saving_inr=saving, wait_cost_inr=cost_w)
                if saving > cost_w:
                    out['action'] = 'sell after ' + ld
    ytd = float(ctx.get('ytd_net_realised_inr') or 0.0)
    if price < float(row.get('average_price') or 0):
        loss = round(sq * (float(row['average_price']) - price), 2)
        out['harvest_inr'] = loss
        out['harvest_note'] = (
            "realising -%s offsets %s of this year's gains" % (_inr(loss), _inr(min(loss, ytd)))
            if ytd >= 0 else
            "adds to this FY's %s net loss (carries forward 8 years)" % _inr(-ytd))
    if out['action'] == 'sell now':
        shield = ' (FY loss shield %s)' % _inr(-ytd) if ytd < 0 and ctx.get('fy_known') else ''
        t = 'after tax: sell now · tax %s%s · cost %s' % (_inr(now['tax']), shield, _inr(now['cost']))
    else:
        t = 'after tax: sell after %s (%d days) · saves %s vs %s expected drift' % (
            ld, out['wait_days'], _inr(out['saving_inr']), _inr(out['wait_cost_inr']))
        if out['lots'] != 'known':
            t += ' · lots: %d of %d dated' % (known_sq, sq)
    out['text'] = t + note + ('' if ctx.get('fy_known') else ' · FY realised unknown')
    return out


def _lots_state(known, total):
    return 'known' if known == total else 'partial' if known else 'unknown'
