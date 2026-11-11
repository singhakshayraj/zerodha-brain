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
    'name': ['name'],
    'mcap': ['mar cap', 'market cap'],
    'pe': ['p/e', 'price to earning'],
    'cash': ['cash eq', 'cash equivalents'],
    'debt': ['debt'],
    'cfo': ['cf opr', 'cash from op', 'cfo'],
    'roce': ['roce', 'return on capital'],
    'roe': ['roe', 'return on equity'],
    'piotroski': ['piotski', 'piotroski'],
    'pledge': ['pledge'],
    'ev_ebitda': ['ev / ebitda', 'ev/ebitda', 'evebitda'],
    'promoter': ['prom. hold', 'promoter hold'],
    'sales_growth_3y': ['sales var 3', 'sales growth 3'],
    'profit_growth_3y': ['profit var 3', 'profit growth 3'],
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
        hdr_i = next(i for i, l in enumerate(lines) if 'name' in l.lower())
    except StopIteration:
        raise ValueError("no header line containing 'Name' found")
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
        if 'name' in l.lower()[:20] and len(split(l)) >= len(headers) - 1:
            continue                         # repeated page header
        cells = split(l)
        if len(cells) < max(cols.values()) + 1:
            continue
        r = {f: cells[i].strip() for f, i in cols.items()}
        for f in r:
            if f != 'name':
                r[f] = _num(r[f])
        if r.get('name'):
            rows.append(r)
    return rows


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
        sym = by_norm.get(n)
        if not sym:
            close = difflib.get_close_matches(n, by_norm, n=1, cutoff=0.85)
            sym = by_norm[close[0]] if close else None
        if sym:
            matched.append({**r, 'symbol': sym,
                            'sector': (universe.get(sym) or {}).get('sector')})
        else:
            unmatched.append(r['name'])
    return matched, unmatched


# ── store ────────────────────────────────────────────────────────────────────

def merge_store(existing: dict, rows: list, asof: str = None) -> dict:
    """Merge parsed+matched rows into the store dict (per-symbol overwrite,
    other symbols kept)."""
    asof = asof or date.today().isoformat()
    store = {'asof': asof, 'by_symbol': dict((existing or {}).get('by_symbol') or {})}
    for r in rows:
        rec = {k: v for k, v in r.items() if k not in ('name', 'symbol', 'sector')}
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


def is_stale(rec: dict, today: str = None, stale_days: int = STALE_DAYS) -> bool:
    today = date.fromisoformat(today) if today else date.today()
    try:
        return (today - date.fromisoformat(rec['asof'])).days > stale_days
    except Exception:
        return True
