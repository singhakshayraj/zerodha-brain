"""One-off/maintenance: seed stock_universe with the Nifty 500 pin.

Reads config.NIFTY500_UNIVERSE (data/nifty500.csv) and bulk-upserts rows keyed
on symbol. Idempotent — re-run after each quarterly reconstitution. Writes
ONLY identity/classification columns; brain_score and trade stats owned by
the paper engine are never touched (upsert merges columns, doesn't replace
the row).

    SUPABASE_URL=... SUPABASE_SERVICE_KEY=... python3 scripts/seed_nifty500_universe.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import config           # noqa: E402
import database as db   # noqa: E402


def universe_rows() -> list:
    """Pure mapping: pinned CSV rows → stock_universe upsert rows."""
    return [{
        'symbol': r['symbol'],
        'exchange': 'NSE',
        'company_name': r.get('company_name'),
        'sector': r.get('sector'),
        'industry': r.get('industry'),
        'instrument_token': r['instrument_token'],
        'is_nifty500': True,
        'is_active': True,
    } for r in config.NIFTY500_UNIVERSE]


if __name__ == '__main__':
    rows = universe_rows()
    if not rows:
        print("no rows — is data/nifty500.csv present?")
        sys.exit(1)
    n = db.upsert_stock_universe_bulk(rows)
    print(f"seeded {n}/{len(rows)} universe rows")
