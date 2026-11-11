"""Thesis engine — advisor v2, SHADOW MODE (P-47, spec 2026-11-11).

One 12-month thesis per holding: action in {HOLD, ADD, TRIM, EXIT}, a target
weight, a sized quantity delta, a fundamentals quality score, a reason and a
falsifier. Pure: no I/O, no orders. Stored at indicators.thesis beside the
unchanged v1 verdict; graded separately by scripts/grade_thesis.py.

Rules (operator defaults, 2026-11-11): max loss −20% from cost unless the
business is intact on fundamentals; max 10% in one name; max 25% in one
sector; ADD only into ADD-grade businesses with fresh fundamentals.
"""
import fundamentals

HORIZON_DAYS = 252
MAX_LOSS_PCT = -20.0
MAX_SINGLE_NAME_PCT = 10.0
MAX_SECTOR_PCT = 25.0
ADD_TARGET_PCT = 5.0
QUALITY_INTACT = 60
QUALITY_ADD = 70
QUALITY_WEAK = 40
FINANCIAL_SECTORS = {'Financial Services'}
ETF_HINTS = ('BEES', 'ETF', 'SENSEX', 'NIFTY', 'IETF', 'GOLD', 'SILVER')


def is_etf(symbol: str) -> bool:
    s = (symbol or '').upper()
    return any(h in s for h in ETF_HINTS)


def quality_score(f: dict, sector: str = None):
    """0–100 from whatever fundamentals are present (spec table); None if
    fewer than 3 scored fields. Financials: ROE instead of ROCE, no
    cash/debt test."""
    if not f:
        return None
    financial = sector in FINANCIAL_SECTORS
    got, maxpts, n = 0.0, 0.0, 0

    ret = f.get('roe') if financial else f.get('roce')
    if ret is None and not financial:
        ret = f.get('roe')
    if ret is not None:
        got += 25 if ret >= 20 else 18 if ret >= 15 else 8 if ret >= 10 else 0
        maxpts += 25; n += 1
    p = f.get('piotroski')
    if p is not None:
        got += 20 if p >= 7 else 15 if p >= 6 else 8 if p >= 5 else 0
        maxpts += 20; n += 1
    if not financial and f.get('cash') is not None and f.get('debt') is not None:
        cash, debt = f['cash'], f['debt']
        got += 20 if cash >= debt else 10 if debt <= 2 * cash else 0
        maxpts += 20; n += 1
    if f.get('cfo') is not None:
        got += 15 if f['cfo'] > 0 else 0
        maxpts += 15; n += 1
    pl = f.get('pledge')
    if pl is not None:
        got += 10 if pl == 0 else 6 if pl < 5 else 0
        maxpts += 10; n += 1
    pe = f.get('pe')
    if pe is not None:
        got += 0 if pe <= 0 else 10 if pe <= 15 else 6 if pe <= 25 else 3 if pe <= 40 else 0
        maxpts += 10; n += 1
    if n < 3 or not maxpts:
        return None
    return int(round(got / maxpts * 100))


def _qty_for_weight(target_pct: float, weight_pct: float, qty: int) -> int:
    """Shares to add (+) or sell (−) to move from weight_pct to target_pct."""
    if not weight_pct or not qty:
        return 0
    return int(round(qty * (target_pct / weight_pct - 1)))


def thesis_for(h: dict, f: dict, today: str = None) -> dict:
    """One holding -> thesis (before the book-level sector pass).
    h: {symbol, quantity, pnl_percent, weight_pct, sector, mom_12_1}
    f: that symbol's fundamentals record or None."""
    sym, qty = h['symbol'], h.get('quantity') or 0
    pnl = h.get('pnl_percent')
    w = h.get('weight_pct') or 0.0
    mom = h.get('mom_12_1')
    etf = is_etf(sym)
    q = None if etf else quality_score(f, h.get('sector'))
    fresh = bool(f) and not fundamentals.is_stale(f, today)
    out = {'action': 'HOLD', 'target_weight_pct': round(w, 1), 'qty_delta': 0,
           'quality': q, 'fundamentals_asof': (f or {}).get('asof'),
           'fundamentals_fresh': fresh, 'confidence': 'low' if (q is None and not etf) else 'normal',
           'horizon_days': HORIZON_DAYS, 'reason': '', 'falsifier': '',
           'rule': None}
    loss_breach = pnl is not None and pnl <= MAX_LOSS_PCT
    intact = q is not None and q >= QUALITY_INTACT

    if etf:
        if w > MAX_SINGLE_NAME_PCT:
            out.update(action='TRIM', rule='single_name_cap',
                       target_weight_pct=MAX_SINGLE_NAME_PCT,
                       qty_delta=_qty_for_weight(MAX_SINGLE_NAME_PCT, w, qty),
                       reason=f"{sym} is {w:.1f}% of the book; cap is {MAX_SINGLE_NAME_PCT:.0f}% — allocation rule, not a view on the asset",
                       falsifier="none — a size cap is a rule, not a forecast")
        else:
            out.update(reason=f"Index/ETF position at {w:.1f}% — allocation only; no business to assess",
                       falsifier="exceeds the single-name cap")
        return out

    if loss_breach and not intact:
        why = 'unknown — no fundamentals pasted' if q is None else f'quality {q} < {QUALITY_INTACT}'
        out.update(action='EXIT', rule='max_loss', target_weight_pct=0.0, qty_delta=-qty,
                   reason=f"Down {pnl:.0f}% vs cost, past the {MAX_LOSS_PCT:.0f}% loss budget, and the business is not proven intact ({why})",
                   falsifier=f"fresh fundamentals scoring ≥ {QUALITY_INTACT} would convert this to HOLD")
        return out
    if q is not None and q < QUALITY_WEAK:
        if mom is not None and mom < 0:
            out.update(action='EXIT', rule='weak_business', target_weight_pct=0.0, qty_delta=-qty,
                       reason=f"Weak business (quality {q}) and 12-1 momentum {mom:+.0f}% — neither the numbers nor the tape support holding",
                       falsifier=f"quality back above {QUALITY_WEAK} on the next results, or 12-1 momentum turning positive")
        else:
            out.update(action='TRIM', rule='weak_business', target_weight_pct=round(w / 2, 1),
                       qty_delta=-max(1, qty // 2) if qty else 0,
                       reason=f"Weak business (quality {q}); tape not yet against it — halve the position",
                       falsifier=f"quality ≥ {QUALITY_WEAK} on the next results restores a full HOLD")
        return out
    if w > MAX_SINGLE_NAME_PCT:
        out.update(action='TRIM', rule='single_name_cap', target_weight_pct=MAX_SINGLE_NAME_PCT,
                   qty_delta=_qty_for_weight(MAX_SINGLE_NAME_PCT, w, qty),
                   reason=f"{w:.1f}% of the book in one name; cap is {MAX_SINGLE_NAME_PCT:.0f}%" + (f" (quality {q})" if q is not None else ''),
                   falsifier="none — a size cap is a rule, not a forecast")
        return out
    if (q is not None and q >= QUALITY_ADD and w < ADD_TARGET_PCT and not loss_breach
            and fresh and mom is not None and mom > 0):
        out.update(action='ADD', rule='add_quality', target_weight_pct=ADD_TARGET_PCT,
                   qty_delta=_qty_for_weight(ADD_TARGET_PCT, w, qty) if w else 0,
                   reason=f"Quality {q} business, 12-1 momentum {mom:+.0f}%, only {w:.1f}% of the book — build to {ADD_TARGET_PCT:.0f}%",
                   falsifier=f"quality < {QUALITY_INTACT} on the next results, or a close more than {abs(MAX_LOSS_PCT):.0f}% below cost")
        return out
    if q is None:
        out.update(reason=f"No fundamentals for {sym} — paste a Screener table (scripts/ingest_fundamentals.py); holding on price alone" + (f", down {pnl:.0f}%" if pnl is not None and pnl < 0 else ''),
                   falsifier=f"a close {abs(MAX_LOSS_PCT):.0f}% below cost with no fundamentals = EXIT")
    else:
        out.update(reason=f"Business intact (quality {q}); {w:.1f}% of the book" + (f", down {pnl:.0f}% — inside the loss budget" if pnl is not None and pnl < 0 else ''),
                   falsifier=f"quality < {QUALITY_INTACT} on the next results, or a close {abs(MAX_LOSS_PCT):.0f}% below cost with quality < {QUALITY_INTACT}")
    return out


def thesis_book(holdings: list, store: dict, today: str = None) -> dict:
    """Whole book -> {symbol: thesis}. Per-holding rules, then the sector cap:
    sectors over MAX_SECTOR_PCT are trimmed lowest-quality-first until under."""
    by_symbol = (store or {}).get('by_symbol') or {}
    out = {h['symbol']: thesis_for(h, by_symbol.get(h['symbol']), today) for h in holdings}
    sectors = {}
    for h in holdings:
        sectors.setdefault(h.get('sector') or 'Unknown', []).append(h)
    for sector, hs in sectors.items():
        if sector == 'Unknown':
            continue
        weight = sum(out[h['symbol']]['target_weight_pct'] for h in hs)
        if weight <= MAX_SECTOR_PCT:
            continue
        excess = weight - MAX_SECTOR_PCT
        ranked = sorted(hs, key=lambda h: (out[h['symbol']]['quality'] is not None,
                                           out[h['symbol']]['quality'] or 0))
        for h in ranked:
            if excess <= 0:
                break
            t = out[h['symbol']]
            cut = min(excess, t['target_weight_pct'])
            if cut <= 0:
                continue
            new_target = round(t['target_weight_pct'] - cut, 1)
            w = h.get('weight_pct') or 0.0
            t.update(action='EXIT' if new_target <= 0 else 'TRIM', rule='sector_cap',
                     target_weight_pct=new_target,
                     qty_delta=_qty_for_weight(new_target, w, h.get('quantity') or 0) if w else 0,
                     reason=f"{sector} is {weight:.0f}% of the book; cap is {MAX_SECTOR_PCT:.0f}% — trimmed lowest-quality first" + (f" (quality {t['quality']})" if t['quality'] is not None else ' (no fundamentals)'),
                     falsifier="none — a sector cap is a rule, not a forecast")
            excess -= cut
    return out


def attach_theses(rows: list, store: dict, sector_map: dict, today: str = None) -> int:
    """Attach indicators.thesis to each advice row IN PLACE. Additive only:
    nothing else on the row is touched. Returns theses attached."""
    holdings = []
    for r in rows:
        ind = r.get('indicators') or {}
        holdings.append({'symbol': r['symbol'], 'quantity': r.get('quantity'),
                         'pnl_percent': r.get('pnl_percent'),
                         'weight_pct': ind.get('portfolio_weight_pct'),
                         'sector': sector_map.get(r['symbol']),
                         'mom_12_1': ind.get('mom_12_1')})
    theses = thesis_book(holdings, store, today)
    n = 0
    for r in rows:
        if isinstance(r.get('indicators'), dict) and r['symbol'] in theses:
            r['indicators']['thesis'] = theses[r['symbol']]
            n += 1
    return n
