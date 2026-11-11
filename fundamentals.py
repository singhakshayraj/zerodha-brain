"""Company fundamentals from a pasted Screener.in table (watchlist or screen).

No API exists for this data; the operator copies a Screener results table
from the browser and runs `scripts/ingest_fundamentals.py`. Parsed rows are
stored in app_config['fundamentals_latest'] as
    {'asof': 'YYYY-MM-DD', 'by_symbol': {SYM: {field: number, 'asof': ...}}}
and read by the thesis engine. Pure parsing here; the store helpers are the
only I/O.
"""
import csv
import difflib
import json
import os
import re
from datetime import date

ROOT = os.path.dirname(os.path.abspath(__file__))
UNIVERSE_CSV = os.path.join(ROOT, 'data', 'nifty500.csv')
STORE_KEY = 'fundamentals_latest'
STALE_DAYS = 45

# canonical field -> substrings identifying Screener's column header (lower)
ALIASES = {
    'name': ['name', 'company'],
    'mcap': ['mar cap', 'market cap'],
    'pe': ['p/e', 'price to earning'],
    'cash': ['cash eq', 'cash equivalents', 'cash rs'],
    'debt': ['debt'],
    'cfo': ['cf opr', 'cf operations', 'cash from op', 'cfo'],
    'roce': ['roce', 'return on capital'],
    'roe': ['roe', 'return on equity'],
    'piotroski': ['piotski', 'piotroski'],
    'pledge': ['pledge'],
    'ev_ebitda': ['ev / ebitda', 'ev/ebitda', 'evebitda'],
    'promoter': ['prom. hold', 'promoter hold'],
    'sales_growth_3y': ['sales var 3', 'sales growth 3'],
    'profit_growth_3y': ['profit var 3', 'profit growth 3'],
    # extended export (scripts/ingest_fundamentals.py, second pass)
    'symbol': ['symbol'],
    'sales_growth_5y': ['sales growth 5'],
    'profit_growth_5y': ['profit growth 5'],
    'qtr_sales_growth': ['qtr sales growth', 'quarterly sales'],
    'qtr_profit_growth': ['qtr profit growth', 'quarterly profit'],
    'opm': ['opm % latest', 'opm'],
    'opm_last_year': ['opm % last year'],
    'avg_roce_5y': ['avg roce', 'average return on capital'],
    'avg_roe_5y': ['avg roe', 'average return on equity'],
    'eps': ['eps latest', 'eps'],
    'eps_last_year': ['eps last year'],
    'interest_coverage': ['interest cov'],
    'price_to_sales': ['price to sales'],
    'price_to_book': ['price to book', 'cmp / bv'],
    'div_yield': ['dividend yield'],
    'peg': ['peg'],
    'high_52w': ['high 52', '52w high', 'high price'],
    'low_52w': ['low 52', '52w low', 'low price'],
    'cagr_1y': ['cagr 1y', 'return over 1year'],
    'cagr_3y': ['cagr 3y', 'return over 3years'],
    'cagr_5y': ['cagr 5y', 'return over 5years'],
    'down_from_high': ['down from 52'],
    'up_from_low': ['up from 52'],
    'promoter_change_3y': ['change promoter', 'change in prom'],
    'fii': ['fii holding'],
    'fii_change': ['change fii'],
    'dii': ['dii holding'],
    'dii_change': ['change dii'],
    'reserves': ['reserves'],
    'working_capital_days': ['working capital days'],
    'debtor_days': ['debtor days'],
    'ccc_days': ['cash conversion'],
    'div_payout': ['dividend payout'],
    'net_profit': ['net profit latest annual'],
    'sales': ['sales latest annual'],
    'net_profit_qtr': ['net profit latest qtr'],
    'ev': ['enterprise value'],
    # banks / NBFCs (financials are scored on these, not cash vs debt)
    'gnpa': ['gross npa', 'gnpa'],
    'nnpa': ['net npa', 'nnpa'],
    'nim': ['net interest margin', 'nim'],
    'casa': ['casa'],
    'car': ['capital adequacy', 'crar'],
    'inventory_turnover': ['inventory turnover'],
}


def _num(s):
    s = (s or '').replace(',', '').replace('%', '').strip()
    try:
        return float(s)
    except ValueError:
        return None


def map_headers(headers: list) -> dict:
    """{canonical: column index} for every recognised header."""
    out = {}
    for i, h in enumerate(headers):
        hl = h.lower().strip()
        for field, keys in ALIASES.items():
            if field in out:
                continue
            if field in ('opm', 'eps', 'high_52w', 'low_52w') and 'last year' in hl:
                continue
            if field in ('fii', 'dii', 'promoter') and 'change' in hl:
                continue
            if field in ('roce', 'roe') and ('avg' in hl or 'average' in hl):
                continue                     # 5y averages have their own field
            if field == 'debt' and 'debtor' in hl:
                continue                     # 'Debtor days' is not debt
            if field == 'nim' and hl.strip() not in ('nim', 'nim %') and 'net interest' not in hl:
                continue                     # 'nim' only as a whole header
            if field in ('gnpa', 'nnpa') and 'change' in hl:
                continue
            if field == 'cash' and 'free cash' in hl:
                continue                     # FCF is not cash on hand
            if field == 'debt' and ('/' in hl or 'equity' in hl):
                continue                     # 'Debt / Eq' is a ratio
            if field == 'roe' and 'roce' in hl:
                continue
            if any(k in hl for k in keys):
                out[field] = i
                break
    return out


def parse_paste(text: str, required: list = ('name',)) -> list:
    """Screener table copied from the browser -> [{field: value}]. Tab- or
    multi-space-separated; the header is the first line containing 'Name';
    repeated header lines (one per page) are skipped. Raises ValueError
    naming any `required` field whose column is missing."""
    lines = [l for l in text.splitlines() if l.strip()]
    try:
        hdr_i = next(i for i, l in enumerate(lines)
                     if 'name' in l.lower() or 'company' in l.lower())
    except StopIteration:
        raise ValueError("no header line containing 'Name'/'Company' found")
    split = (lambda l: l.split('\t')) if '\t' in lines[hdr_i] \
        else (lambda l: re.split(r'\s{2,}', l.strip()))
    headers = split(lines[hdr_i])
    cols = map_headers(headers)
    missing = [f for f in required if f not in cols]
    if missing:
        raise ValueError(f"paste is missing columns {missing}; "
                         f"headers seen: {headers}")
    rows = []
    for l in lines[hdr_i + 1:]:
        cells = split(l)
        if cells[0].strip().lower() == headers[0].strip().lower():
            continue                         # repeated page header
        if len(cells) < max(cols.values()) + 1:
            continue
        r = {f: cells[i].strip() for f, i in cols.items()}
        for f in r:
            if f not in ('name', 'symbol'):
                r[f] = _num(r[f])
        if r.get('name'):
            rows.append(r)
    return rows


# Screener's display names that neither exact nor fuzzy matching resolves.
SCREENER_NAMES = {
    'bhel': 'BHEL',
    'indianrenewable': 'IREDA',
    'icicilifeinsurance': 'ICICILIFE',
    'mazagondock': 'MAZDOCK',
    'shyammetalics': 'SHYAMMETL',
    'suprajitengg': 'SUPRAJIT',
    'abb': 'ABB',
    'adanienergysol': 'ADANIENSOL',
    'adanienterp': 'ADANIENT',
    'adaniports': 'ADANIPORTS',
    'adityaamc': 'ABSLAMC',
    'adityabirlacap': 'ABCAPITAL',
    'apollohospitals': 'APOLLOHOSP',
    'bajajholdings': 'BAJAJHLDNG',
    'bajajhousing': 'BAJAJHFL',
    'bankofmaha': 'MAHABANK',
    'bpcl': 'BPCL',
    'camsservices': 'CAMS',
    'canarahsbc': 'CANHLIFE',
    'cdsl': 'CDSL',
    'centralbank': 'CENTRALBK',
    'cholafinancial': 'CHOLAHLDNG',
    'cholamaninvandfn': 'CHOLAFIN',
    'cpcl': 'CHENNPETRO',
    'deepakfertilis': 'DEEPAKFERT',
    'fact': 'FACT',
    'geshippingco': 'GESHIP',
    'gmdc': 'GMDCLTD',
    'godrejconsumer': 'GODREJCP',
    'gujaratfluoroch': 'FLUOROCHEM',
    'hdfcamc': 'HDFCAMC',
    'hegadvancedmaterials': 'HEGAM',
    'hindunilever': 'HINDUNILVR',
    'hpcl': 'HINDPETRO',
    'hudco': 'HUDCO',
    'iciciamc': 'ICICIAMC',
    'icicilombard': 'ICICIGI',
    'indianenergyex': 'IEX',
    'iocl': 'IOC',
    'irctc': 'IRCTC',
    'jindalstain': 'JSL',
    'jiofinancial': 'JIOFIN',
    'jppowerven': 'JPPOWER',
    'jubilantfood': 'JUBLFOOD',
    'mandm': 'M&M',
    'mandmfinserv': 'M&MFIN',
    'maxfinancial': 'MFSL',
    'motiloswalfin': 'MOTILALOFS',
    'mrpl': 'MRPL',
    'nbcc': 'NBCC',
    'one97': 'PAYTM',
    'ongc': 'ONGC',
    'sail': 'SAIL',
    'sbi': 'SBIN',
    'sbicards': 'SBICARD',
    'sbifundsmgt': 'SBIFM',
    'sbilifeinsuran': 'SBILIFE',
    'supremeinds': 'SUPREMEIND',
    'tatacomm': 'TATACOMM',
    'tataconsumer': 'TATACONSUM',
    'tatamotors': 'TATAMOTORS',
    'tatamotorspveh': 'TMPV',
    'tatatelemah': 'TTML',
    'tcs': 'TCS',
    'titancompany': 'TITAN',
    'torrentpharma': 'TORNTPHARM',
    'urbancompany': 'URBANCO',
    'utiamc': 'UTIAMC',
}


def _norm(name: str) -> str:
    n = name.lower().replace('&', 'and')
    n = re.sub(r'\b(ltd|limited|the)\b', '', n)
    return re.sub(r'[^a-z0-9]', '', n)


def load_universe(path: str = UNIVERSE_CSV) -> dict:
    with open(path) as f:
        return {r['symbol']: r for r in csv.DictReader(f)}


def match_symbols(rows: list, universe: dict, extra_names: dict = None):
    """Attach 'symbol' by company name (exact normalised, then fuzzy ≥0.85).
    extra_names: {symbol: company_name} beyond the Nifty 500 (e.g. holdings).
    Returns (matched_rows, unmatched_names)."""
    by_norm = {_norm(u['company_name']): s for s, u in universe.items()}
    for s, n in (extra_names or {}).items():
        by_norm.setdefault(_norm(n), s)
    matched, unmatched = [], []
    for r in rows:
        n = _norm(r['name'])
        sym = r.get('symbol') or SCREENER_NAMES.get(n) or by_norm.get(n)
        if not sym:
            close = difflib.get_close_matches(n, by_norm, n=1, cutoff=0.85)
            sym = by_norm[close[0]] if close else None
        if not sym and len(n) >= 6:          # Screener truncates: 'Suprajit Engg.'
            pref = [s for k, s in by_norm.items() if k.startswith(n[:6])]
            sym = pref[0] if len(pref) == 1 else None
        if sym:
            matched.append({**r, 'symbol': sym,
                            'sector': (universe.get(sym) or {}).get('sector')})
        else:
            unmatched.append(r['name'])
    return matched, unmatched


# ── store ────────────────────────────────────────────────────────────────────

def merge_store(existing: dict, rows: list, asof: str = None) -> dict:
    """Merge parsed+matched rows into the store dict, field by field: a
    second paste with different columns (the extended export) adds to a
    symbol's record instead of replacing it. None values never overwrite."""
    asof = asof or date.today().isoformat()
    store = {'asof': asof, 'by_symbol': dict((existing or {}).get('by_symbol') or {})}
    for r in rows:
        rec = dict(store['by_symbol'].get(r['symbol']) or {})
        rec.update({k: v for k, v in r.items()
                    if k not in ('name', 'symbol', 'sector') and v is not None})
        rec['asof'] = asof
        rec['name'] = r.get('name')
        store['by_symbol'][r['symbol']] = rec
    return store


def load_store() -> dict:
    """{'asof', 'by_symbol'} from app_config, or empty."""
    try:
        import database as db
        raw = db.get_config(STORE_KEY)
        return json.loads(raw) if raw else {'asof': None, 'by_symbol': {}}
    except Exception as e:
        print(f"[fundamentals] load failed (non-fatal): {e}")
        return {'asof': None, 'by_symbol': {}}


def save_store(store: dict) -> None:
    import database as db
    db.write_config(STORE_KEY, json.dumps(store))


SNAPSHOT_TABLE = 'fundamentals_snapshots'


def snapshot_rows(store: dict, symbols, asof: str) -> list:
    """Pure: point-in-time rows for the symbols touched by one ingest."""
    by = (store or {}).get('by_symbol') or {}
    return [{'asof': asof, 'symbol': s,
             'data': {k: v for k, v in by[s].items() if k != 'asof'}}
            for s in sorted(set(symbols)) if s in by]


def save_snapshot(store: dict, symbols, asof: str) -> int:
    """Append (asof, symbol) copies so later grading can use what was known
    then. Same-day re-ingests overwrite that day's copy."""
    import database as db
    rows = snapshot_rows(store, symbols, asof)
    for i in range(0, len(rows), 500):
        db.supabase.table(SNAPSHOT_TABLE).upsert(
            rows[i:i + 500], on_conflict='asof,symbol').execute()
    return len(rows)


def load_asof(symbol: str, on_date: str):
    """Latest snapshot for symbol taken on or before on_date, or None."""
    import database as db
    res = (db.supabase.table(SNAPSHOT_TABLE).select('asof,data')
           .eq('symbol', symbol).lte('asof', on_date)
           .order('asof', desc=True).limit(1).execute().data) or []
    return {**res[0]['data'], 'asof': res[0]['asof']} if res else None


def is_stale(rec: dict, today: str = None, stale_days: int = STALE_DAYS) -> bool:
    today = date.fromisoformat(today) if today else date.today()
    try:
        return (today - date.fromisoformat(rec['asof'])).days > stale_days
    except Exception:
        return True
