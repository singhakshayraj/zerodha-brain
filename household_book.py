"""Household returns, drawdown and net worth [household PMS]. Pure, no DB.

Deposits/withdrawals are not in `pnl`, so they never count as returns (TWR).
The index base of 100 is the value *before* the first row, so the first row's
own return is included (nothing is dropped).
"""
from datetime import date


def _chain(daily, field):
    rows = sorted(daily, key=lambda r: r['date'])
    rs, series, idx = [], [], 100.0
    for r in rows:
        p, vo = r.get(field), r.get('value_open')
        if p is None or vo is None or vo <= 0:
            continue
        rs.append(p / vo)
        idx *= 1 + rs[-1]
        series.append({'date': r['date'], 'index': round(idx, 2)})
    return rs, series


def _ret(rs, n=None):
    out = 1.0
    for r in (rs if n is None else rs[-n:]):
        out *= 1 + r
    return round((out - 1) * 100, 2)


def _twr(daily, field):
    rs, series = _chain(daily, field)
    return {'1m': _ret(rs, 21), '3m': _ret(rs, 63), 'since': _ret(rs),
            'start': series[0]['date'] if series else None, 'series': series}


def twr(daily):
    return _twr(daily, 'pnl')


def benchmark_twr(daily):
    return _twr(daily, 'nifty_pnl')


def xirr(flows):
    """Annualised % (actual/365), or None if no sign change / no convergence."""
    fl = [(date.fromisoformat(d[:10]), a) for d, a in flows]
    if not any(a > 0 for _, a in fl) or not any(a < 0 for _, a in fl):
        return None
    t0 = min(d for d, _ in fl)
    ts = [((d - t0).days / 365.0, a) for d, a in fl]

    def npv(r):
        return sum(a / (1 + r) ** t for t, a in ts)

    def d_npv(r):
        return sum(-t * a / (1 + r) ** (t + 1) for t, a in ts)

    r = 0.1
    for _ in range(100):
        f, df = npv(r), d_npv(r)
        if df == 0:
            break
        nr = r - f / df
        if nr <= -0.99 or nr > 10:
            break
        if abs(nr - r) < 1e-10:
            return round(nr * 100, 2)
        r = nr
    lo, hi = -0.99, 10.0
    if npv(lo) * npv(hi) > 0:
        return None
    for _ in range(200):
        mid = (lo + hi) / 2
        if npv(lo) * npv(mid) <= 0:
            hi = mid
        else:
            lo = mid
    return round((lo + hi) / 2 * 100, 2)


def max_drawdown(series):
    if not series:
        return {'pct': 0.0, 'peak': None, 'trough': None}
    pk = series[0]
    worst = {'pct': 0.0, 'peak': pk['date'], 'trough': pk['date']}
    for p in series:
        if p['index'] > pk['index']:
            pk = p
        dd = (p['index'] / pk['index'] - 1) * 100
        if dd < worst['pct']:
            worst = {'pct': dd, 'peak': pk['date'], 'trough': p['date']}
    worst['pct'] = round(worst['pct'], 2)
    return worst


def net_worth(positions):
    by_acct, by_cls, unpriced = {}, {}, []
    for p in positions:
        px = p.get('last_price')
        if px is None:
            unpriced.append(p['symbol'])
        v = (p.get('quantity') or 0) * (px or 0)
        by_acct[p['account']] = by_acct.get(p['account'], 0) + v
        by_cls[p['asset_class']] = by_cls.get(p['asset_class'], 0) + v
    return {'total': round(sum(by_acct.values()), 2),
            'by_account': {k: round(v, 2) for k, v in by_acct.items()},
            'by_asset_class': {k: round(v, 2) for k, v in by_cls.items()},
            'unpriced': unpriced}


# ---- x-ray vs targets, risk, tax ------------------------------------------
import numpy as np
from datetime import timedelta
from dateutil.relativedelta import relativedelta

import advisor_risk
import costs
import household as _h
import sector_view

DEFAULT_TARGETS = {'sector_cap_pct': _h.SECTOR_CAP_PCT, 'dup_cap_pct': _h.DUP_CAP_PCT,
                   'name_cap_pct': _h.SINGLE_NAME_CAP_PCT, 'etf_cap_pct': _h.HEDGE_SLEEVE_CAP_PCT,
                   'size_tolerance_pp': 10.0, 'source': 'default'}


def _val(p):
    return (p.get('quantity') or 0) * (p.get('last_price') or 0)


def _by_symbol(positions):
    out = {}
    for p in positions:
        d = out.setdefault(p['symbol'], {'value': 0.0, 'accounts': set(), 'cls': p['asset_class']})
        d['value'] += _val(p)
        d['accounts'].add(p['account'])
    return out


def xray(positions, sectors, sizes, index_weights, targets=DEFAULT_TARGETS, index_size_mix=None):
    total = sum(_val(p) for p in positions) or 1.0
    pct = lambda v: round(v / total * 100, 2)
    sec, siz, names, etf = {}, {}, [], 0.0
    for sym, d in _by_symbol(positions).items():
        g = sector_view.group_of(sym, (sectors or {}).get(sym))
        sec[g] = sec.get(g, 0.0) + d['value']
        z = (sizes or {}).get(sym, 'micro')
        siz[z] = siz.get(z, 0.0) + d['value']
        if d['cls'] == 'etf_gold_silver' or sector_view.is_etf(sym):
            etf += d['value']
        n = pct(d['value'])
        br = ('name' if n > targets['name_cap_pct']
              else 'overlap' if len(d['accounts']) > 1 and n > targets['dup_cap_pct'] else None)
        names.append({'symbol': sym, 'pct': n, 'accounts': sorted(d['accounts']), 'breach': br})
    sectors_out = [{'name': g, 'pct': pct(v), 'index_pct': (index_weights or {}).get(g, 0.0),
                    'target_pct': targets['sector_cap_pct'],
                    'drift_pp': round(pct(v) - (index_weights or {}).get(g, 0.0), 2),
                    'breach': pct(v) > targets['sector_cap_pct'] and g not in _h.NOT_SECTORS}
                   for g, v in sec.items()]
    mix = index_size_mix or {}
    sizes_out = [{'size': z, 'pct': pct(v), 'index_pct': mix.get(z, 0.0),
                  'drift_pp': round(pct(v) - mix.get(z, 0.0), 2),
                  'breach': abs(pct(v) - mix.get(z, 0.0)) > targets['size_tolerance_pp']}
                 for z, v in siz.items()]
    for z, ip in mix.items():
        if z not in siz:
            sizes_out.append({'size': z, 'pct': 0.0, 'index_pct': ip, 'drift_pp': -ip,
                              'breach': ip > targets['size_tolerance_pp']})
    for rows, key in ((sectors_out, 'pct'), (names, 'pct'), (sizes_out, 'pct')):
        rows.sort(key=lambda r: r[key], reverse=True)
    return {'sectors': sectors_out, 'sizes': sizes_out, 'names': names,
            'etf_pct': pct(etf), 'etf_breach': pct(etf) > targets['etf_cap_pct']}


def risk(positions, closes_by_symbol, lookback=60):
    held = {s: d for s, d in _by_symbol(positions).items() if d['cls'] != 'fund_unconfirmed'}
    rets, excluded = {}, []
    for s in held:
        c = np.asarray((closes_by_symbol or {}).get(s) or [], dtype=float)[-(lookback + 1):]
        if len(c) < 31 or (c[:-1] == 0).any():
            excluded.append(s)
        else:
            rets[s] = np.diff(c) / c[:-1]
    if len(rets) < 2:
        return {'effective_bets': None, 'top_contributors': [], 'clusters': [],
                'n_used': len(rets), 'excluded': excluded}
    # ponytail: closes carry no dates; aligned on the common tail, upgrade if gaps matter
    n = min(len(r) for r in rets.values())
    syms = list(rets)
    X = np.column_stack([rets[s][-n:] for s in syms])
    tot = sum(held[s]['value'] for s in syms) or 1.0
    w = np.array([held[s]['value'] / tot for s in syms])
    cov, _ = advisor_risk.ledoit_wolf_cov(X)
    rc = advisor_risk.risk_contributions(cov, w)
    top = sorted(({'symbol': s, 'risk_pct': round(float(rc['pctr'][i]) * 100, 1),
                   'value_pct': round(float(w[i]) * 100, 1)} for i, s in enumerate(syms)),
                 key=lambda r: r['risk_pct'], reverse=True)[:5]
    corr = advisor_risk._correlation_read(
        [{'symbol': s, 'weight_pct': w[i] * 100} for i, s in enumerate(syms)], closes_by_symbol)
    return {'effective_bets': round(float(advisor_risk.effective_number_of_bets(cov, w)), 2),
            'top_contributors': top, 'clusters': (corr or {}).get('clusters', []),
            'n_used': len(syms), 'excluded': excluded}


def tax_summary(positions, lots_by_account, fy_by_account, today):
    today_s = today.isoformat()
    horizon = today + timedelta(days=90)
    by_acct, cal, harvest, unmatched = {}, [], [], {}
    for p in positions:
        a, sym, q = p['account'], p['symbol'], int(p.get('quantity') or 0)
        lots = ((lots_by_account or {}).get(a) or {}).get(sym) or []
        r = costs.after_tax_pct(lots, q, p['last_price'], today_s)
        acc = by_acct.setdefault(a, {'stcg': 0.0, 'ltcg': 0.0})
        acc['stcg'] += r['stcg_inr']
        acc['ltcg'] += r['ltcg_inr']
        if r['unmatched_qty'] > 0:
            unmatched.setdefault(a, []).append(sym)
        left = q
        for lot in lots:
            take = min(left, lot['qty'])
            if take <= 0:
                break
            left -= take
            on = date.fromisoformat(lot['date'][:10]) + relativedelta(years=1) + timedelta(days=1)
            if today < on <= horizon:
                cal.append({'symbol': sym, 'account': a, 'date': on.isoformat(), 'qty': take})
        if p['last_price'] < p['avg_price']:
            harvest.append({'symbol': sym, 'account': a,
                            'loss_inr': round(q * (p['avg_price'] - p['last_price']), 2)})
    cal.sort(key=lambda r: r['date'])
    harvest.sort(key=lambda r: r['loss_inr'], reverse=True)
    return {'fy_realised': {a: (c.get('ytd_net_realised_inr') if c.get('fy_known') else None)
                            for a, c in (fy_by_account or {}).items()},
            'unrealised_stcg': round(sum(v['stcg'] for v in by_acct.values()), 2),
            'unrealised_ltcg': round(sum(v['ltcg'] for v in by_acct.values()), 2),
            'by_account': {a: {k: round(x, 2) for k, x in v.items()} for a, v in by_acct.items()},
            'ltcg_calendar': cal, 'harvest': harvest, 'unmatched': unmatched}
