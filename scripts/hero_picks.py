"""Hero picks (contrarian value) — LOCAL, advisory-only research tool.

Merges a pasted Screener.in screen (company financials) with our own daily
price history (data/daily_history.pkl + `candles` interval='day' in Supabase)
and ranks stocks the market is against but whose balance sheet says it is
too pessimistic. Writes the list to app_config 'hero_picks_latest' (shown
on /advisor) and first-seen picks to 'hero_picks_log' for 12-month grading.
Never places orders.

WEEKLY WORKFLOW
  1. python3 scripts/ingest_fundamentals.py <screener pastes>   (monthly)
  2. python3 scripts/hero_picks.py            # reads the ingested store
     (or: python3 scripts/hero_picks.py screen.txt to use a raw paste)
  3. Output: ranked table on stdout, data/hero_picks_<date>.csv, app_config

SCREENER QUERY (Screener auto-suggests field names; fix any it flags):
    Market Capitalization > 5000 AND
    Cash Equivalents > Debt AND
    Return on capital employed > 15 AND
    Piotroski score >= 6 AND
    Pledged percentage < 5 AND
    Cash from operations last year > 0 AND
    Return over 1year < 10

COLUMNS TO SHOW on the screen (Edit columns):
    Market Capitalization, Price to Earning, Cash Equivalents, Debt,
    Cash from operations last year, Return on capital employed,
    Piotroski score, Pledged percentage, EVEBITDA

THE FOUR TESTS (all must pass)
  1. Market against it  — >=35% below 3y high, OR 3y return >=25 pts behind Nifty 50
  2. Cash cushion       — cash >= debt, operating cash flow > 0
  3. Good business      — ROCE >= 15, Piotroski >= 6, pledge < 5%
  4. Not a falling knife— 1y low is >= 63 trading days old AND last-3-month
                          return no worse than Nifty's by more than 5 pts
  Banks/NBFCs (sector 'Financial Services') are excluded: cash/debt mean
  something else for lenders.

RANK: mean percentile of earnings yield (1/PE), net cash / market cap, and
cheap EV/EBITDA (if present). Top 8, max 2 per sector.
"""
import csv
import os
import pickle
import sys
from datetime import date

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PKL = os.path.join(ROOT, 'data', 'daily_history.pkl')
UNIVERSE = os.path.join(ROOT, 'data', 'nifty500.csv')
INDEX = 'NIFTY 50'

sys.path.insert(0, ROOT)
import fundamentals  # noqa: E402  shared Screener parser + name matching

REQUIRED = ['name', 'mcap', 'pe', 'cash', 'debt', 'cfo', 'roce',
            'piotroski', 'pledge']
TOP_N, PER_SECTOR = 8, 2


def parse_paste(text: str) -> list:
    return fundamentals.parse_paste(text, required=REQUIRED)


def load_universe() -> dict:
    return fundamentals.load_universe(UNIVERSE)


def match_symbols(rows: list, universe: dict):
    return fundamentals.match_symbols(rows, universe)


def load_closes(symbols: list) -> dict:
    """{symbol: [(date_str, close), ...]} ascending: local 5y pickle, then
    the DB's daily bars (2026-08-26 onward)."""
    hist = pickle.load(open(PKL, 'rb'))
    out = {}
    for s in symbols:
        bars = hist.get(f'NSE:{s}') or hist.get(s) or []
        out[s] = {str(b['timestamp'])[:10]: float(b['close']) for b in bars}
    try:
        sys.path.insert(0, ROOT)
        import database
        for s in symbols:
            off = 0
            while True:
                page = (database.supabase.table('candles')
                        .select('trade_date,close').eq('interval', 'day')
                        .eq('symbol', s).order('trade_date')
                        .range(off, off + 999).execute().data) or []
                for b in page:
                    out[s][str(b['trade_date'])] = float(b['close'])
                if len(page) < 1000:
                    break
                off += 1000
    except Exception as e:
        print(f"[hero_picks] DB bars unavailable ({e}); pickle only (ends 2026-08-25)")
    return {s: sorted(d.items()) for s, d in out.items()}


def price_metrics(closes: list, index_closes: list) -> dict:
    """Contrarian + falling-knife metrics from daily closes (~252 bars/yr)."""
    c = [x for _, x in closes]
    ix = [x for _, x in index_closes]
    if len(c) < 260 or len(ix) < 260:
        return None
    n3 = min(len(c), len(ix), 756)
    ret = lambda xs, k: xs[-1] / xs[-k] - 1
    last_year = c[-252:]
    low_age = len(last_year) - 1 - last_year.index(min(last_year))
    return {
        'dd_3y': c[-1] / max(c[-n3:]) - 1,
        'rel_3y': ret(c, n3) - ret(ix, n3),
        'rel_3m': ret(c, 63) - ret(ix, 63),
        'low_age_days': low_age,
        'last_date': closes[-1][0],
        'last_close': c[-1],
    }


def gates(r: dict) -> dict:
    """Each test -> bool. r carries Screener fields + price metrics."""
    return {
        'market_against': r['dd_3y'] <= -0.35 or r['rel_3y'] <= -0.25,
        'cash_cushion': (r['cash'] or 0) >= (r['debt'] or 0) and (r['cfo'] or 0) > 0,
        'good_business': ((r['roce'] or 0) >= 15 and (r['piotroski'] or 0) >= 6
                          and r['pledge'] is not None and r['pledge'] < 5),
        'not_falling_knife': r['low_age_days'] >= 63 and r['rel_3m'] >= -0.05,
    }


def _pct_rank(vals: list) -> list:
    if len(vals) == 1:
        return [1.0]
    order = sorted(range(len(vals)), key=lambda i: vals[i])
    out = [0.0] * len(vals)
    for rank, i in enumerate(order):
        out[i] = rank / max(len(vals) - 1, 1)
    return out


def rank(passers: list) -> list:
    """Mean percentile of cheapness signals; top N with a per-sector cap."""
    if not passers:
        return []
    ey = [1 / r['pe'] if r['pe'] and r['pe'] > 0 else 0 for r in passers]
    nc = [((r['cash'] or 0) - (r['debt'] or 0)) / r['mcap'] if r['mcap'] else 0
          for r in passers]
    parts = [_pct_rank(ey), _pct_rank(nc)]
    if all(r.get('ev_ebitda') for r in passers):
        parts.append(_pct_rank([-r['ev_ebitda'] for r in passers]))
    for i, r in enumerate(passers):
        r['score'] = round(sum(p[i] for p in parts) / len(parts), 3)
        r['net_cash_pct_mcap'] = round(nc[i] * 100, 1)
    picks, per = [], {}
    for r in sorted(passers, key=lambda r: -r['score']):
        if per.get(r['sector'], 0) < PER_SECTOR:
            picks.append(r)
            per[r['sector']] = per.get(r['sector'], 0) + 1
        if len(picks) == TOP_N:
            break
    return picks


def rows_from_store(universe: dict) -> list:
    """Nifty-500 records from app_config fundamentals_latest (the ingested
    Screener pastes), in the same shape parse_paste+match_symbols produce."""
    st = fundamentals.load_store().get('by_symbol') or {}
    out = []
    for sym, rec in st.items():
        if sym in universe and all(rec.get(k) is not None for k in REQUIRED[1:]):
            out.append({**rec, 'symbol': sym, 'sector': universe[sym]['sector']})
    return out


def run(text: str = None):
    universe = load_universe()
    if text:
        matched, unmatched = match_symbols(parse_paste(text), universe)
    else:
        matched, unmatched = rows_from_store(universe), []
    rows = matched
    matched = [r for r in matched if r['sector'] != 'Financial Services']
    closes = load_closes([r['symbol'] for r in matched] + [INDEX])
    idx = closes.pop(INDEX)
    scored, near = [], []
    for r in matched:
        m = price_metrics(closes.get(r['symbol'], []), idx)
        if not m:
            continue
        r.update(m)
        g = gates(r)
        r['failed'] = [k for k, ok in g.items() if not ok]
        (scored if not r['failed'] else near).append(r)
    return rank(scored), near, unmatched, len(rows)


PICKS_KEY, LOG_KEY = 'hero_picks_latest', 'hero_picks_log'
PUBLISH_FIELDS = ['symbol', 'name', 'sector', 'score', 'dd_3y', 'rel_3y', 'rel_3m',
                  'low_age_days', 'pe', 'mcap', 'cash', 'debt', 'cfo', 'roce',
                  'piotroski', 'pledge', 'ev_ebitda', 'net_cash_pct_mcap',
                  'last_close', 'last_date', 'failed']


def publish(picks: list, near: list, n: int, today: str = None) -> dict:
    """Write the list for /advisor and append first-seen picks to the entry
    log (symbol, entry_date, entry_price) for 12-month grading. Returns the
    payload."""
    import json
    import database as db
    today = today or date.today().isoformat()
    slim = lambda r: {k: r.get(k) for k in PUBLISH_FIELDS if k in r}
    one_off = sorted((r for r in near if len(r['failed']) == 1),
                     key=lambda r: r['dd_3y'])
    payload = {'asof': today, 'n_screened': n,
               'gates': ['market_against', 'cash_cushion', 'good_business', 'not_falling_knife'],
               'picks': [slim(r) for r in picks], 'near_misses': [slim(r) for r in one_off[:20]]}
    db.write_config(PICKS_KEY, json.dumps(payload, default=str))
    try:
        log = json.loads(db.get_config(LOG_KEY) or '[]')
    except Exception:
        log = []
    seen = {e['symbol'] for e in log}
    for r in picks:
        if r['symbol'] not in seen:
            log.append({'symbol': r['symbol'], 'sector': r['sector'], 'entry_date': today,
                        'entry_price': r.get('last_close'), 'score': r['score']})
    db.write_config(LOG_KEY, json.dumps(log))
    return payload


def main():
    if len(sys.argv) > 2:
        sys.exit(__doc__)
    picks, near, unmatched, n = run(open(sys.argv[1]).read() if len(sys.argv) == 2 else None)
    publish(picks, near, n)
    print(f"\nScreen rows: {n} | not in Nifty 500 / unmatched: {len(unmatched)} "
          f"| hero picks: {len(picks)}\n")
    fmt = "{:<12} {:<28} {:>6} {:>8} {:>8} {:>7} {:>8} {:>6}"
    print(fmt.format('SYMBOL', 'SECTOR', 'SCORE', 'OFF-3YHI', 'VS-NIFTY', 'P/E',
                     'NETCASH%', 'ROCE'))
    for r in picks:
        print(fmt.format(r['symbol'], r['sector'][:28], r['score'],
                         f"{r['dd_3y']*100:.0f}%", f"{r['rel_3y']*100:.0f}pt",
                         r['pe'] or '-', r['net_cash_pct_mcap'], r['roce']))
    one_off = [r for r in near if len(r['failed']) == 1]
    if one_off:
        print("\nNear misses (failed one test):")
        for r in one_off:
            print(f"  {r['symbol']:<12} failed {r['failed'][0]}")
    if unmatched:
        print(f"\nSkipped (outside Nifty 500 or name mismatch): {', '.join(unmatched[:15])}")
    out = os.path.join(ROOT, 'data', f'hero_picks_{date.today().isoformat()}.csv')
    keys = ['symbol', 'name', 'sector', 'score', 'dd_3y', 'rel_3y', 'rel_3m',
            'low_age_days', 'pe', 'mcap', 'cash', 'debt', 'cfo', 'roce',
            'piotroski', 'pledge', 'ev_ebitda', 'net_cash_pct_mcap', 'last_date']
    with open(out, 'w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=keys, extrasaction='ignore')
        w.writeheader()
        w.writerows(picks)
    print(f"\nSaved {out}")


if __name__ == '__main__':
    main()
