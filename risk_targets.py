"""Risk-based construction targets [P-68 phase 2]: capped HRP weights, ENB floor,
regime-scaled rebalance step. Pure: no DB, no network. Advisory only."""
import math

import numpy as np

import advisor_risk
import sector_view
from household import NOT_SECTORS
from household_book import DEFAULT_TARGETS

ENB_FLOOR = 6
STEP_CALM = 0.5
STEP_DRIFTED = 0.25
LOOKBACK = 60
REGIME_DAYS = 250
MIN_COMMON = 31
_EPS = 1e-9


def _capacity(groups, m, sector_cap, etf_cap):
    """Most weight the book can hold under the active caps (None = cap off)."""
    by = {}
    for g, e in groups.values():
        by.setdefault((g, e), 0)
        by[(g, e)] += 1
    tot = 0.0
    for (g, e), n in by.items():
        c = n * m
        if e and etf_cap is not None:
            c = min(c, etf_cap)
        elif g not in NOT_SECTORS and sector_cap is not None:
            c = min(c, sector_cap)
        tot += c
    return tot


def cap_weights(w, groups, caps):
    """w {sym: frac}, groups {sym: (sector_group, is_etf)}, caps = DEFAULT_TARGETS-style.
    -> ({sym: frac} sums to 1, infeasible cap names among 'name','sector','etf')."""
    name_c = caps['name_cap_pct'] / 100
    sec_c = caps['sector_cap_pct'] / 100
    etf_c = caps['etf_cap_pct'] / 100
    active = {'name': name_c, 'sector': sec_c, 'etf': etf_c}
    infeasible = []

    def _cap(a):
        return _capacity(groups, a['name'] or 1.0, a['sector'], a['etf'])

    for k in ('name', 'sector', 'etf'):        # each cap judged alone
        if _cap({j: (active[j] if j == k else None) for j in active}) < 1 - _EPS:
            active[k] = None
            infeasible.append(k)
    for k in ('sector', 'name', 'etf'):        # then the combination; drop until it fits
        if _cap(active) >= 1 - _EPS:
            break
        if active[k] is not None:
            active[k] = None
            infeasible.append(k)
    m = active['name'] or 1.0
    x = {s: float(v) for s, v in w.items()}
    tot = sum(x.values()) or 1.0
    x = {s: v / tot for s, v in x.items()}
    frozen = set()
    for _ in range(len(x) * 3 + 5):
        viol = False
        for s in x:
            if s not in frozen and x[s] > m + _EPS:
                x[s] = m
                frozen.add(s)
                viol = True
        buckets = {}
        for s, (g, e) in groups.items():
            if e and active['etf'] is not None:
                buckets.setdefault(('etf',), (active['etf'], []))[1].append(s)
            elif not e and g not in NOT_SECTORS and active['sector'] is not None:
                buckets.setdefault((g,), (active['sector'], []))[1].append(s)
        for cap, members in buckets.values():
            t = sum(x[s] for s in members)
            if t > cap + _EPS:
                for s in members:
                    x[s] *= cap / t
                    frozen.add(s)
                viol = True
        free = [s for s in x if s not in frozen]
        room = 1.0 - sum(x[s] for s in frozen)
        base = sum(x[s] for s in free)
        if not free or base <= 0:
            break
        for s in free:
            x[s] = x[s] / base * room
        if not viol:
            break
    return x, infeasible


def sell_qty_to_target(qty, price, current_pct, target_pct, book_value, step):
    """Whole shares moving `step` of the way from current to target; 0 if at/below target."""
    if not price or price <= 0 or current_pct <= target_pct:
        return 0
    n = math.floor(step * (current_pct - target_pct) / 100 * book_value / price)
    return int(max(0, min(int(qty or 0), n)))


def _empty(excluded):
    return {'asof_close': None, 'targets': [], 'excluded': sorted(excluded), 'enb_now': None,
            'enb_target': None, 'enb_floor': ENB_FLOOR, 'below_floor': False,
            'regime': None, 'step': STEP_CALM, 'caps_infeasible': []}


def build(positions, closes_by_symbol, sectors, caps=None):
    caps = caps or DEFAULT_TARGETS
    held = {}
    for p in positions or []:
        if p.get('asset_class') in ('equity', 'etf_gold_silver'):
            v = (p.get('quantity') or 0) * (p.get('last_price') or 0)
            held[p['symbol']] = held.get(p['symbol'], 0.0) + v
    # same alignment + thinnest-history exclusion as household_book.risk
    excluded = sorted(s for s, v in held.items() if v <= 0)     # unpriced / zero-qty: drop just them
    held = {s: v for s, v in held.items() if v > 0}
    series = {s: (closes_by_symbol or {}).get(s) or {} for s in held}
    for s in [s for s, d in series.items() if any(float(c) == 0 for c in d.values())]:
        excluded.append(s)
        del series[s]
    common = set()
    while series:
        common = set.intersection(*(set(v) for v in series.values()))
        if len(common) >= MIN_COMMON or len(series) < 2:
            break
        worst = min(series, key=lambda s: len(series[s]))
        excluded.append(worst)
        del series[worst]
    if len(series) < 2 or len(common) < MIN_COMMON:
        return _empty(excluded)
    dates = sorted(common)[-(REGIME_DAYS + 1):]   # regime needs history beyond the 60d window
    syms = list(series)
    closes = {s: [float(series[s][d]) for d in dates] for s in syms}
    X = np.column_stack([np.diff(closes[s]) / np.array(closes[s][:-1]) for s in syms])
    tot = sum(held[s] for s in syms)
    cur = np.array([held[s] / tot for s in syms])
    cov, _ = advisor_risk.ledoit_wolf_cov(X[-LOOKBACK:])
    raw = advisor_risk.hierarchical_risk_parity(cov)
    groups = {s: (sector_view.group_of(s, (sectors or {}).get(s)), sector_view.is_etf(s)) for s in syms}
    capped, infeasible = cap_weights({s: raw[i] for i, s in enumerate(syms)}, groups, caps)
    tgt = np.array([capped[s] for s in syms])
    pctr = advisor_risk.risk_contributions(cov, cur)['pctr']
    enb_now = round(float(advisor_risk.effective_number_of_bets(cov, cur)), 2)
    enb_tgt = round(float(advisor_risk.effective_number_of_bets(cov, tgt)), 2)
    gw = {}
    for i, s in enumerate(syms):
        gw[groups[s][0]] = gw.get(groups[s][0], 0.0) + cur[i]
    sleeve = max(gw, key=gw.get)
    mask = np.array([groups[s][0] == sleeve for s in syms])
    reg = advisor_risk.sleeve_correlation_regime(X, mask, cur, LOOKBACK)
    regime = None if reg['corr_full'] is None else {
        'drifted': reg['drifted'], 'corr_full': reg['corr_full'], 'corr_recent': reg['corr_recent']}
    step = STEP_DRIFTED if regime and regime['drifted'] else STEP_CALM
    targets = [{'symbol': s, 'current_pct': round(float(cur[i]) * 100, 2),
                'target_pct': round(float(tgt[i]) * 100, 2),
                'drift_pp': round(float(cur[i] - tgt[i]) * 100, 2),
                'risk_pct': round(float(pctr[i]) * 100, 1)} for i, s in enumerate(syms)]
    return {'asof_close': dates[-1], 'targets': targets, 'excluded': sorted(excluded),
            'enb_now': enb_now, 'enb_target': enb_tgt, 'enb_floor': ENB_FLOOR,
            'below_floor': enb_now < ENB_FLOOR, 'regime': regime, 'step': step,
            'caps_infeasible': infeasible}
