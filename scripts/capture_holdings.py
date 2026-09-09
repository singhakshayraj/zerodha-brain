#!/usr/bin/env python3
"""Capture one holdings snapshot on demand (Task 2).

Needs a token (holdings read); no trading session required. Fail-loud: a dead
token or API error writes NOTHING.

Usage:  python3 scripts/capture_holdings.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import database as db
import holdings_snapshot
from kite_client import KiteClient
from market_data import MarketData


def main() -> int:
    token = db.get_enc_token()
    if not token:
        print("[capture_holdings] no enc_token present — paste one, then re-run. "
              "Nothing written.")
        return 1
    md = MarketData(KiteClient(token))
    try:
        res = holdings_snapshot.capture_snapshot(md, source='cli')
    except Exception as e:
        print(f"[capture_holdings] FAILED — nothing written: {type(e).__name__}: {e}")
        return 1
    print(f"[capture_holdings] OK: {res}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
