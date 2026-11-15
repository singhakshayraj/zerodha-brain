"""[P-57] Due diligence on a rotation target before the SELL→BUY shows:
red flags from the fundamentals store and any recent tagged news. Display
only — never changes the target, the sizing or the verdict. Attached as
row['indicators']['target_dd'] = {'symbol', 'flags': [{'flag','severity',
'detail'}], 'checked', 'asof'}."""
from datetime import datetime, timedelta

import pytz

IST = pytz.timezone('Asia/Kolkata')
NEWS_DAYS = 14
NEWS_NEG = -0.2
FINANCIAL = {'Financial Services'}


def flags_from_fundamentals(f: dict, sector: str = None) -> list:
    """Pure. f = store['by_symbol'][symbol] (or None)."""
    if not f:
        return [{'flag': 'no_fundamentals', 'severity': 'amber', 'detail': 'no Screener record on file'}]
    out = []

    def add(flag, sev, detail):
        out.append({'flag': flag, 'severity': sev, 'detail': detail})

    pl = f.get('pledge')
    if pl:
        add('pledge', 'red' if pl >= 10 else 'amber', f"promoters pledged {pl:g}%")
    pc = f.get('promoter_change_3y')
    if pc is not None and pc <= -5:
        add('promoter_selling', 'red', f"promoter holding down {abs(pc):g}pp in 3y")
    pr = f.get('promoter')
    if pr and pr < 25:                       # 0 = no promoter group (L&T, ITC): not a flag
        add('low_promoter', 'amber', f"promoter holding {pr:g}%")
    fc = f.get('fii_change')
    if fc is not None and fc <= -2:
        add('fii_exit', 'amber', f"FII holding down {abs(fc):g}pp")
    if sector not in FINANCIAL:
        ic = f.get('interest_coverage')
        if ic is not None and ic < 2:
            add('interest_coverage', 'red', f"interest cover {ic:g}x")
        cfo, debt, cash = f.get('cfo'), f.get('debt'), f.get('cash')
        if cfo is not None and cfo < 0:
            add('negative_cfo', 'red' if (debt or 0) > 2 * (cash or 0) else 'amber',
                f"operating cash flow ₹{cfo:,.0f} cr" + (" with debt > 2× cash" if (debt or 0) > 2 * (cash or 0) else ""))
    qp, qs = f.get('qtr_profit_growth'), f.get('qtr_sales_growth')
    if qp is not None and qp <= -25:
        add('quarter_profit', 'amber', f"last quarter profit {qp:g}%")
    if qs is not None and qs <= -20:
        add('quarter_sales', 'amber', f"last quarter sales {qs:g}%")
    pe = f.get('pe')
    if pe is not None and pe > 60:
        add('expensive', 'amber', f"P/E {pe:g}")
    up = f.get('up_from_low')
    if up is not None and up >= 100:
        add('chasing', 'amber', f"{up:g}% above its 52-week low")
    return out


def flags_from_news(symbol: str, news: list) -> list:
    """Pure. news: rows of news_events (symbols, headline, sentiment_score,
    published_at) already limited to the recent window."""
    bad = [n for n in news if symbol in (n.get('symbols') or [])
           and (n.get('sentiment_score') or 0) <= NEWS_NEG]
    return [{'flag': 'negative_news', 'severity': 'red',
             'detail': f"{str(n.get('published_at'))[:10]}: {n.get('headline')}"} for n in bad[:2]]


def recent_news() -> list:
    import database as db
    since = (datetime.now(IST) - timedelta(days=NEWS_DAYS)).isoformat()
    try:
        return (db.supabase.table('news_events').select('symbols,headline,sentiment_score,published_at')
                .gte('published_at', since).execute().data) or []
    except Exception:
        return []


def due_diligence(symbol: str, store: dict, sector: str = None, news: list = None) -> dict:
    f = ((store or {}).get('by_symbol') or {}).get(symbol)
    flags = flags_from_fundamentals(f, sector) + flags_from_news(symbol, news or [])
    return {'symbol': symbol, 'flags': flags,
            'checked': 'pledge, promoter & FII moves, leverage, cash flow, last quarter, valuation, 52w stretch, news',
            'asof': (f or {}).get('asof') or (store or {}).get('asof')}


def attach(rows: list, store: dict, sector_of: dict) -> int:
    """Attach to every row with a rotation target. Returns rows flagged."""
    news = recent_news()
    n = 0
    for row in rows:
        t = row.get('rotation_target_symbol')
        if not t:
            continue
        dd = due_diligence(t, store, sector_of.get(t), news)
        row.setdefault('indicators', {})
        if isinstance(row['indicators'], dict):
            row['indicators']['target_dd'] = dd
        n += bool(dd['flags'])
    return n
