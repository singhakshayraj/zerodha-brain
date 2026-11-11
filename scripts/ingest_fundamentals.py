"""Ingest a pasted Screener.in table into app_config['fundamentals_latest'].

    python3 scripts/ingest_fundamentals.py paste.txt [more.txt ...]

Paste source: Screener.in -> a screen or your watchlist -> select the results
table in the browser -> copy -> save to a text file. Columns to show (Edit
columns): Market Capitalization, Price to Earning, Cash Equivalents, Debt,
Cash from operations last year, Return on capital employed, Return on equity,
Piotroski score, Pledged percentage. Extra columns are ignored; a missing one
is reported, not fatal. Names are matched to NSE symbols via data/nifty500.csv
and the latest holdings snapshot; unmatched names are listed.
"""
import os
import sys
from datetime import date

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import fundamentals  # noqa: E402


def holdings_names() -> dict:
    """{symbol: name} from the latest holdings snapshot, for names outside
    the Nifty 500."""
    try:
        import database as db
        return {r['identifier']: r.get('identifier')
                for r in db.get_latest_holdings_snapshot() or []
                if r.get('instrument_type') == 'EQ'}
    except Exception:
        return {}


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    universe = fundamentals.load_universe()
    rows = []
    for path in sys.argv[1:]:
        rows += fundamentals.parse_paste(open(path).read())
    matched, unmatched = fundamentals.match_symbols(rows, universe, holdings_names())
    missing = sorted({f for f in ('roce', 'piotroski', 'cash', 'debt', 'cfo', 'pledge', 'pe')
                      if all(r.get(f) is None for r in matched)})
    store = fundamentals.merge_store(fundamentals.load_store(), matched,
                                     date.today().isoformat())
    fundamentals.save_store(store)
    print(f"parsed {len(rows)} rows, matched {len(matched)}, store now holds "
          f"{len(store['by_symbol'])} symbols (asof {store['asof']})")
    if missing:
        print(f"⚠ no values for {missing} in this paste — add those columns on Screener")
    if unmatched:
        print(f"unmatched names: {', '.join(unmatched[:20])}")


if __name__ == '__main__':
    main()
