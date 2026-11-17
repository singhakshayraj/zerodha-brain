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


def xray(positions, sectors, sizes, index_weights, index_size_mix, targets=DEFAULT_TARGETS):
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
    """closes_by_symbol = {symbol: {'YYYY-MM-DD': close}}; aligned on common dates."""
    held = {s: d for s, d in _by_symbol(positions).items()
            if d['cls'] in ('equity', 'etf_gold_silver')}
    series = {s: (closes_by_symbol or {}).get(s) or {} for s in held}
    excluded = []
    while series:
        common = set.intersection(*(set(v) for v in series.values()))
        if len(common) >= 31 or len(series) < 2:
            break
        # drop the name with the thinnest history, then retry the overlap
        worst = min(series, key=lambda s: len(series[s]))
        excluded.append(worst)
        del series[worst]
    if len(series) < 2:
        return {'effective_bets': None, 'top_contributors': [], 'clusters': [],
                'n_used': len(series), 'excluded': sorted(excluded)}
    dates = sorted(common)[-(lookback + 1):]
    syms = list(series)
    closes = {s: [float(series[s][d]) for d in dates] for s in syms}
    if any(c[0] == 0 or 0 in c[:-1] for c in closes.values()):
        return {'effective_bets': None, 'top_contributors': [], 'clusters': [],
                'n_used': 0, 'excluded': sorted(syms + excluded)}
    X = np.column_stack([np.diff(closes[s]) / np.array(closes[s][:-1]) for s in syms])
    tot = sum(held[s]['value'] for s in syms) or 1.0
    w = np.array([held[s]['value'] / tot for s in syms])
    cov, _ = advisor_risk.ledoit_wolf_cov(X)
    rc = advisor_risk.risk_contributions(cov, w)
    top = sorted(({'symbol': s, 'risk_pct': round(float(rc['pctr'][i]) * 100, 1),
                   'value_pct': round(float(w[i]) * 100, 1)} for i, s in enumerate(syms)),
                 key=lambda r: r['risk_pct'], reverse=True)[:5]
    corr = advisor_risk._correlation_read(
        [{'symbol': s, 'weight_pct': w[i] * 100} for i, s in enumerate(syms)], closes)
    return {'effective_bets': round(float(advisor_risk.effective_number_of_bets(cov, w)), 2),
            'top_contributors': top, 'clusters': (corr or {}).get('clusters', []),
            'n_used': len(syms), 'excluded': sorted(excluded)}


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


# ---- rebalance list --------------------------------------------------------
import math
import tax_actions
import tax_context


def _tax_for(a, sym, qty, px, lots_by_account, fy_by_account, today_s):
    lots = ((lots_by_account or {}).get(a) or {}).get(sym) or []
    r = costs.after_tax_pct(lots, qty, px, today_s)
    return tax_actions.fy_tax(r['stcg_inr'], r['ltcg_inr'], (fy_by_account or {}).get(a) or {})


def rebalance(xray_out, positions, lots_by_account, fy_by_account, targets, today, sectors=None):
    """Whole-share trims that bring each breach inside its cap, from the account
    where the trim is cheapest in tax (never more than held there), plus sector-only
    underweights vs the index. Pure."""
    total = sum(_val(p) for p in positions)
    today_s = today.isoformat()
    held = {(p['account'], p['symbol']): int(p.get('quantity') or 0) for p in positions}
    px = {p['symbol']: p['last_price'] for p in positions if p.get('last_price')}
    trims = []

    def trim(sym, excess_inr, reason):
        need = math.ceil(excess_inr / px[sym])
        accts = [a for (a, s), q in held.items() if s == sym and q > 0]
        # cheapest tax per share first; the remainder (if any) comes from the other account
        per_share = lambda a: _tax_for(a, sym, min(need, held[(a, sym)]), px[sym], lots_by_account,
                                       fy_by_account, today_s) / min(need, held[(a, sym)])
        for a in sorted(accts, key=per_share):
            q = min(need, held[(a, sym)])
            if q <= 0:
                break
            held[(a, sym)] -= q
            need -= q
            freed = round(q * px[sym], 2)
            trims.append({'kind': 'trim', 'symbol': sym, 'account': a, 'qty': q, 'freed_inr': freed,
                          'tax_inr': _tax_for(a, sym, q, px[sym], lots_by_account, fy_by_account, today_s),
                          'cost_inr': round(costs.round_trip_cost_pct(None) / 100 * freed / 2, 2),
                          'reason': reason})

    for n in xray_out['names']:
        if n['breach'] and n['symbol'] in px:
            cap = targets['name_cap_pct'] if n['breach'] == 'name' else targets['dup_cap_pct']
            trim(n['symbol'], (n['pct'] - cap) / 100 * total,
                 f"{n['symbol']} {n['pct']}% > {cap:g}% {'name' if n['breach'] == 'name' else 'overlap'} cap")
    for s in xray_out['sectors']:
        if not s['breach']:
            continue
        done = sum(t['freed_inr'] for t in trims
                   if sector_view.group_of(t['symbol'], (sectors or {}).get(t['symbol'])) == s['name'])
        excess = (s['pct'] - s['target_pct']) / 100 * total - done
        top = sorted(((sym, d['value']) for sym, d in _by_symbol(positions).items()
                      if sym in px and sector_view.group_of(sym, (sectors or {}).get(sym)) == s['name']),
                     key=lambda r: r[1], reverse=True)[:3]
        tv = sum(v for _, v in top) or 1.0
        for sym, v in top:
            if excess > 0:
                trim(sym, excess * v / tv,
                     f"sector {s['name']} {s['pct']}% > {s['target_pct']:g}% cap")
    trims.sort(key=lambda t: t['freed_inr'], reverse=True)
    under = sorted((s for s in xray_out['sectors'] if s['drift_pp'] < 0 and s['name'] not in _h.NOT_SECTORS),
                   key=lambda s: s['drift_pp'])[:3]
    return trims + [{'kind': 'underweight', 'sector': s['name'], 'gap_pp': round(-s['drift_pp'], 2)}
                    for s in under]


# ---- publish (the one DB-touching entry point) -----------------------------
import json
import pytz
from datetime import datetime

import benchmarks
import database as db
from universe_calls import _paged


def _index_size_mix(sizes=None):
    import config
    import fundamentals
    sizes = benchmarks.size_buckets() if sizes is None else sizes
    by = (fundamentals.load_store() or {}).get('by_symbol') or {}
    g = {}
    for u in config.NIFTY500_UNIVERSE:
        m = (by.get(u['symbol']) or {}).get('mcap')
        if m:
            z = sizes.get(u['symbol'], 'micro')
            g[z] = g.get(z, 0.0) + m
    tot = sum(g.values()) or 1.0
    return {k: v / tot * 100 for k, v in g.items()}


def _positions():
    t = db.supabase.table
    top = t('portfolio_advice').select('run_id,run_date').eq('is_official', True) \
        .order('created_at', desc=True).limit(1).execute().data or []
    pos, run_date = [], None
    if top:
        run_date = str(top[0].get('run_date'))
        pos = [{'account': 'primary', 'symbol': r['symbol'], 'quantity': r['quantity'],
                'avg_price': r.get('avg_price'), 'last_price': r.get('last_price'),
                'asset_class': 'etf_gold_silver' if sector_view.is_etf(r['symbol']) else 'equity'}
               for r in _paged(lambda: t('portfolio_advice').select('*').eq('run_id', top[0]['run_id'])
                               .order('symbol'))]
    raw = db.get_config('advisor_natwar_latest')
    try:
        nat = json.loads(raw)['rows']
        assert nat
    except Exception:
        return None, None, 'natwar payload missing or malformed'
    if not top or not pos:
        return None, None, 'no official portfolio_advice run'
    for r in nat:
        pos.append({'account': 'natwar', 'symbol': r['symbol'], 'quantity': r['quantity'],
                    'avg_price': r.get('avg_price', r.get('average_price')), 'last_price': r.get('last_price'),
                    'asset_class': 'etf_gold_silver' if sector_view.is_etf(r['symbol']) else 'equity'})
    unconf = [r['holding_label'] for r in _paged(lambda: t('holding_scheme_map').select('*')
              .eq('confirmed', False).order('holding_label'))]
    pos += [{'account': 'unknown', 'symbol': u, 'quantity': 0, 'avg_price': 0, 'last_price': 0,
             'asset_class': 'fund_unconfirmed'} for u in unconf]
    return pos, unconf, run_date


def _flows(rows):
    """Windowed flows from pnl_daily rows: value_open on day 1, each day's
    sells - buys (value_open(D) = value_close(D-1), so nothing is double-counted),
    value_close on the last day. Returns {date: amount}."""
    rows = sorted(rows, key=lambda r: r['date'])
    out = {}
    if not rows:
        return out
    add = lambda d, v: out.__setitem__(d, out.get(d, 0.0) + v)
    add(rows[0]['date'], -float(rows[0].get('value_open') or 0))
    for r in rows:
        v = float(r.get('sells_inr') or 0) - float(r.get('buys_inr') or 0)
        if v:
            add(r['date'], v)
    add(rows[-1]['date'], float(rows[-1].get('value_close') or 0))
    return out


def _returns(accounts):
    t = db.supabase.table
    by_acct, comb, flows = {}, {}, {}
    for a in accounts:
        by_acct[a] = _paged(lambda: t('pnl_daily').select('date,pnl,value_open,value_close,buys_inr,sells_inr,nifty_pnl')
                            .eq('account', a).order('date'))
        flows[a] = _flows(by_acct[a])
        for r in by_acct[a]:
            c = comb.setdefault(r['date'], {'date': r['date'], 'pnl': 0.0, 'value_open': 0.0, 'nifty_pnl': 0.0})
            for k in ('pnl', 'value_open', 'nifty_pnl'):
                c[k] += float(r.get(k) or 0)
    by_acct['household'] = sorted(comb.values(), key=lambda r: r['date'])
    hh = {}
    for a in accounts:
        for d, v in flows[a].items():
            hh[d] = hh.get(d, 0.0) + v
    flows['household'] = hh
    return by_acct, flows


def _xirr_window(fl):
    if not fl or (date.fromisoformat(max(fl)) - date.fromisoformat(min(fl))).days < 30:
        return None
    return xirr(sorted(fl.items()))


def publish(today=None):
    today = today or datetime.now(pytz.timezone('Asia/Kolkata')).date()
    today_s = today.isoformat()
    positions, unconf, run_date = _positions()
    if positions is None:
        print(f'[household] skipped: {run_date}')    # run_date carries the reason
        return {'skipped': run_date}
    nw = net_worth(positions)
    held = [p for p in positions if p['account'] != 'unknown' and p.get('quantity')]
    accounts = sorted({p['account'] for p in held})
    syms = sorted({p['symbol'] for p in held})

    ctx = {a: tax_context.context_for(a, today_s, sorted({p['symbol'] for p in held
                                                                       if p['account'] == a}))
           for a in accounts}
    lots = {a: c['lots_by_symbol'] for a, c in ctx.items()}
    fy = {a: c['fy'] for a, c in ctx.items()}
    tax = tax_summary(held, lots, fy, today)

    since = (today - timedelta(days=100)).isoformat()
    closes = {}
    for r in _paged(lambda: db.supabase.table('candles').select('symbol,trade_date,close')
                    .eq('interval', 'day').in_('symbol', syms).gte('trade_date', since)
                    .order('trade_date').order('symbol')) if syms else []:
        if r.get('close') is not None:
            closes.setdefault(r['symbol'], {})[str(r['trade_date'])] = float(r['close'])

    daily, flows = _returns(accounts)
    returns = {}
    for k, d in daily.items():
        tw, bm = twr(d), benchmark_twr(d)
        returns[k] = {**{f: v for f, v in tw.items()}, **{f'bench_{f}': v for f, v in bm.items()},
                      'max_drawdown': max_drawdown(tw['series']), 'xirr': _xirr_window(flows[k]),
                      'xirr_partial': True, 'xirr_since': d[0]['date'] if d else None,
                      'xirr_note': f"window since {d[0]['date'] if d else None}, excludes dividends"}

    raw = db.get_config('household_targets')
    targets = {**DEFAULT_TARGETS, **json.loads(raw)} if raw else dict(DEFAULT_TARGETS)
    if not raw:
        db.write_config('household_targets', json.dumps(DEFAULT_TARGETS))

    sectors, sizes = benchmarks.sectors(), benchmarks.size_buckets()
    xr = xray(held, sectors, sizes, _h._index_weights(), _index_size_mix(sizes), targets)
    out = {'asof': today_s, 'net_worth': nw, 'returns': returns, 'xray': xr,
           'risk': risk(held, closes), 'tax': tax,
           'rebalance': rebalance(xr, held, lots, fy, targets, today, sectors),
           'funds_unconfirmed': unconf, 'targets_source': targets.get('source', 'operator'),
           'sections_asof': {'positions': run_date,
                             'returns': (daily['household'][-1]['date'] if daily['household'] else None),
                             'xirr_since': (daily['household'][0]['date'] if daily['household'] else None),
                             'tax': today_s,
                             'risk': max((d for v in closes.values() for d in v), default=None)}}
    db.write_config('household_book_latest', json.dumps(out, default=str))
    return out
