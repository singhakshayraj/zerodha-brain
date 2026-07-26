"""Whole-book portfolio risk — concentration, measured return-correlation, and
tax-loss-harvest. Pure (no I/O, no orders). Extracted from portfolio_advisor
(SE4 module split) and re-exported there for backward compatibility.

The reads a per-symbol scorer is structurally blind to: one name dominating the
book, a sector (or a measured correlation cluster) that is really one bet at Nx
size, and underwater exit-flagged names whose sale realizes a capital loss.
"""
import numpy as np

CONCENTRATION_FLAG_PCT = 25.0
SECTOR_CONCENTRATION_PCT = 35.0   # a sector this heavy = correlated over-bet

# portfolio_risk v2 — measured return correlation (supersedes the sector
# proxy where daily closes are available; catches cross-sector co-movement
# and 'Unknown'-sector blind spots).
CORR_LOOKBACK_DAYS = 60        # trailing daily returns window for the matrix
CORR_MIN_OVERLAP = 30          # min aligned returns to trust a pair's corr
CORR_CLUSTER_THRESHOLD = 0.7   # pairwise corr at/above this = "move together"
CORR_CLUSTER_FLAG_PCT = 30.0   # a correlated cluster this heavy earns a flag


def _correlation_read(positions: list, closes_by_symbol: dict) -> dict:
    """Measured return-correlation view over the holdings that have daily
    history — the real version of the sector proxy. Pure. Builds each name's
    trailing daily returns, correlates them, and reports:
      - clusters: connected groups of names all pairwise corr >= threshold
        (these move as one bet regardless of sector label),
      - effective_bets: 1 / (w·C·w) with weights normalized over covered
        names — the correlation-adjusted count of independent positions
        (equals N when uncorrelated, collapses toward 1 as the book co-moves),
      - top_pairs: the most-correlated name pairs, for transparency.
    Returns None when fewer than 2 names have a trustworthy aligned window."""
    weight_of = {p['symbol']: p['weight_pct'] for p in positions}
    series = {}
    for p in positions:
        sym = p['symbol']
        closes = (closes_by_symbol or {}).get(sym)
        if not closes or len(closes) < CORR_MIN_OVERLAP + 1:
            continue
        arr = np.asarray(closes[-(CORR_LOOKBACK_DAYS + 1):], dtype=float)
        if len(arr) < CORR_MIN_OVERLAP + 1 or (arr[:-1] == 0).any():
            continue
        series[sym] = np.diff(arr) / arr[:-1]
    if len(series) < 2:
        return None

    # Align every series to the shortest common tail so all pairs share dates.
    n = min(len(r) for r in series.values())
    if n < CORR_MIN_OVERLAP:
        return None
    # Drop flat (zero-variance) series — correlation is undefined for them.
    syms = [s for s in series if np.std(series[s][-n:]) > 0]
    syms.sort(key=lambda s: weight_of.get(s, 0.0), reverse=True)
    if len(syms) < 2:
        return None

    corr = np.corrcoef(np.vstack([series[s][-n:] for s in syms]))
    corr = np.clip(np.nan_to_num(corr, nan=0.0), -1.0, 1.0)

    # effective number of independent bets (correlation-only proxy).
    w = np.array([weight_of.get(s, 0.0) for s in syms], dtype=float)
    effective_bets = None
    if w.sum() > 0:
        wn = w / w.sum()
        denom = float(wn @ corr @ wn)
        if denom > 0:
            effective_bets = round(1.0 / denom, 1)

    # Connected components on the (corr >= threshold) graph = correlation
    # clusters. A cluster of >=2 names is a group moving together.
    adj = corr >= CORR_CLUSTER_THRESHOLD
    seen, clusters = set(), []
    for i in range(len(syms)):
        if i in seen:
            continue
        stack, comp = [i], []
        while stack:
            j = stack.pop()
            if j in seen:
                continue
            seen.add(j)
            comp.append(j)
            for k in range(len(syms)):
                if k != j and k not in seen and adj[j][k]:
                    stack.append(k)
        if len(comp) >= 2:
            members = [syms[i] for i in comp]
            pair_c = [corr[a][b] for x, a in enumerate(comp) for b in comp[x + 1:]]
            clusters.append({
                'symbols': members,
                'weight_pct': round(sum(weight_of.get(m, 0.0) for m in members), 1),
                'avg_corr': round(float(np.mean(pair_c)), 2),
            })
    clusters.sort(key=lambda c: c['weight_pct'], reverse=True)

    pairs = [{'symbols': [syms[a], syms[b]], 'corr': round(float(corr[a][b]), 2)}
             for a in range(len(syms)) for b in range(a + 1, len(syms))]
    pairs.sort(key=lambda x: x['corr'], reverse=True)

    return {
        'lookback_days': CORR_LOOKBACK_DAYS,
        'window_returns': int(n),
        'names_covered': len(syms),
        'effective_bets': effective_bets,
        'clusters': clusters,
        'top_pairs': pairs[:5],
    }


def portfolio_risk(advice_rows: list, sector_map: dict = None,
                   closes_by_symbol: dict = None) -> dict:
    """Whole-book risk view — the layer per-name scoring is structurally
    blind to. Pure. Three reads a holdings advisor needs but a per-symbol
    scorer can't give:

    - Single-name concentration (one position dominating the book).
    - Sector concentration, the cheap robust proxy for correlation: same-
      sector names move together, so three PSU banks is closer to one bet
      at 3x size than three independent positions. When daily closes are
      passed (closes_by_symbol), a measured return-correlation read (v2)
      supersedes this proxy — clustering names by actual co-movement and
      reporting effective_bets — and its clusters append to the flags here;
      the sector view stays as the zero-history fallback.
    - Tax-loss harvest candidates: underwater names the advisor ALREADY
      wants to exit (SELL/TRIM). Selling both acts on the weak trend and
      realizes a capital loss that offsets gains elsewhere — real rupees for
      a red book. (India: STT-paid equity losses offset capital gains; the
      short- vs long-term split depends on holding period, which we don't
      assert here.)

    sector_map: {symbol: sector}. closes_by_symbol: {symbol: [daily closes,
    oldest first]} to enable the v2 correlation read (optional — omit for the
    sector-proxy-only behaviour). Rows without value or with INSUFFICIENT data
    are ignored for weighting but never crash the read."""
    sector_map = sector_map or {}
    positions, total = [], 0.0
    for r in advice_rows or []:
        qty = r.get('quantity') or 0
        last = r.get('last_price') or 0
        val = qty * last
        if val <= 0:
            continue
        positions.append({
            'symbol': r.get('symbol'), 'value': val, 'quantity': qty,
            'sector': sector_map.get(r.get('symbol')),
            'pnl_percent': r.get('pnl_percent'),
            'avg_price': r.get('avg_price'), 'last_price': last,
            'verdict': r.get('verdict'),
        })
        total += val

    empty = {'total_value': 0.0, 'top_position': None, 'sector_weights': {},
             'concentration_flags': [], 'tax_loss_harvest': [],
             'harvestable_loss_inr': 0.0, 'correlation': None}
    if not positions or total <= 0:
        return empty

    for p in positions:
        p['weight_pct'] = round(p['value'] / total * 100, 1)
    top = max(positions, key=lambda p: p['weight_pct'])

    sector_weights = {}
    for p in positions:
        s = p['sector'] or 'Unknown'
        sector_weights[s] = round(sector_weights.get(s, 0.0) + p['weight_pct'], 1)

    flags = []
    if top['weight_pct'] >= CONCENTRATION_FLAG_PCT:
        flags.append(f"{top['symbol']} is {top['weight_pct']:.0f}% of the book "
                     f"— single-name concentration")
    for s, w in sorted(sector_weights.items(), key=lambda kv: kv[1], reverse=True):
        members = [p['symbol'] for p in positions
                   if (p['sector'] or 'Unknown') == s]
        if s != 'Unknown' and w >= SECTOR_CONCENTRATION_PCT and len(members) >= 2:
            flags.append(
                f"{s} is {w:.0f}% of the book across {len(members)} names "
                f"({', '.join(members)}) — correlated exposure, effectively "
                f"one bet at ~{len(members)}x size")

    # Measured return-correlation (v2): supersedes the sector proxy where
    # daily candles are available. Cross-sector co-movement and 'Unknown'
    # names the sector map can't cluster are caught here.
    correlation = _correlation_read(positions, closes_by_symbol)
    if correlation:
        for cl in correlation['clusters']:
            if cl['weight_pct'] >= CORR_CLUSTER_FLAG_PCT:
                flags.append(
                    f"{', '.join(cl['symbols'])} move together "
                    f"(avg corr {cl['avg_corr']:.2f}) — {cl['weight_pct']:.0f}% of "
                    f"the book behaving as ~1 bet, not "
                    f"{len(cl['symbols'])} independent positions")

    harvest, harvestable = [], 0.0
    for p in positions:
        pnl = p['pnl_percent']
        avg = p.get('avg_price') or 0
        if (pnl is not None and pnl < 0
                and p['verdict'] in ('SELL', 'SELL_ON_BOUNCE', 'TRIM')
                and avg > p['last_price']):
            loss = (avg - p['last_price']) * p['quantity']
            if loss > 0:
                harvest.append({'symbol': p['symbol'],
                                'unrealized_loss_inr': round(loss, 2),
                                'verdict': p['verdict'],
                                'pnl_percent': pnl})
                harvestable += loss

    return {
        'total_value': round(total, 2),
        'top_position': {'symbol': top['symbol'],
                         'weight_pct': top['weight_pct']},
        'sector_weights': sector_weights,
        'concentration_flags': flags,
        'tax_loss_harvest': sorted(
            harvest, key=lambda x: x['unrealized_loss_inr'], reverse=True),
        'harvestable_loss_inr': round(harvestable, 2),
        'correlation': correlation,
    }


def build_portfolio_risk_lines(risk: dict) -> list:
    """Telegram digest lines for the portfolio-level read. Empty list when
    there's nothing flag-worthy — keeps the push channel quiet on a clean,
    well-diversified book."""
    if not risk or not risk.get('total_value'):
        return []
    body = [f"⚠ {f}" for f in risk.get('concentration_flags', [])]
    corr = risk.get('correlation')
    if corr and corr.get('effective_bets') is not None and corr.get('clusters'):
        groups = '; '.join('+'.join(c['symbols']) for c in corr['clusters'])
        body.append(
            f"🔗 Effective bets: ~{corr['effective_bets']} of "
            f"{corr['names_covered']} — names moving together ({groups}) count "
            f"as fewer independent positions than the raw holding count.")
    harvest = risk.get('tax_loss_harvest') or []
    if harvest:
        names = ', '.join(
            f"{x['symbol']} (−₹{abs(x['unrealized_loss_inr']):,.0f})"
            for x in harvest[:4])
        body.append(
            f"🧾 Tax-loss harvest: the weak names already flagged for exit "
            f"realize ~₹{risk['harvestable_loss_inr']:,.0f} in capital losses "
            f"({names}) — offsets capital gains elsewhere (short/long-term "
            f"split depends on holding period).")
    return (["", "Portfolio-level:"] + body) if body else []
