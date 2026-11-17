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
