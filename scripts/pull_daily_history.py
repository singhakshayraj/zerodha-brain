#!/usr/bin/env python3
"""One-time-ish pull of daily bars for the Nifty 500 + the Nifty 50 benchmark
into a local cache, so advisor experiments can replay history offline.

WHY a local cache: advisor_scoring.advise() is pure, so any scoring variant can
be re-run over any past date given that date's candles. Daily bars are never
revised, so one pull serves every future experiment — and the replay needs no
token, which matters because the enc_token dies ~04:34 IST daily.

Resumable: re-running only fetches symbols not already cached.

Usage:
  python3 scripts/pull_daily_history.py            # fetch missing symbols
  python3 scripts/pull_daily_history.py --refresh  # refetch everything
"""
import csv
import os
import pickle
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import database as db
import market_data
from advisor_scoring import NIFTY50_INDEX_TOKEN
from kite_client import KiteClient

CACHE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     'data', 'daily_history.pkl')
DAYS = 1900  # Kite caps day-interval history at 2000 days
PACE_S = 0.3
BENCHMARK = 'NSE:NIFTY 50'


def load_cache() -> dict:
    if os.path.exists(CACHE):
        with open(CACHE, 'rb') as f:
            return pickle.load(f)
    return {}


def save_cache(cache: dict) -> None:
    tmp = CACHE + '.tmp'
    with open(tmp, 'wb') as f:
        pickle.dump(cache, f)
    os.replace(tmp, CACHE)


def main() -> int:
    refresh = '--refresh' in sys.argv
    cache = {} if refresh else load_cache()
    print(f"cache: {len(cache)} symbols already held")

    md = market_data.MarketData(KiteClient(db.get_enc_token()))
    with open(os.path.join('data', 'nifty500.csv')) as f:
        universe = [(r['symbol'], int(r['instrument_token']))
                    for r in csv.DictReader(f)]
    targets = [(BENCHMARK, NIFTY50_INDEX_TOKEN)] + universe

    fetched = failed = skipped = 0
    for i, (sym, token) in enumerate(targets, 1):
        if sym in cache and cache[sym]:
            skipped += 1
            continue
        md._instrument_cache[sym] = token
        try:
            bars = md.get_candles(sym, 'day', days=DAYS) or []
            if bars:
                cache[sym] = bars
                fetched += 1
            else:
                failed += 1
                print(f"  [{i}/{len(targets)}] {sym}: no bars")
        except Exception as e:
            failed += 1
            print(f"  [{i}/{len(targets)}] {sym}: {type(e).__name__} {str(e)[:90]}")
        if fetched and fetched % 50 == 0:
            save_cache(cache)
            print(f"  ...{i}/{len(targets)} fetched={fetched} failed={failed}")
        time.sleep(PACE_S)

    save_cache(cache)
    depth = sorted(len(v) for v in cache.values())
    print(f"\ndone: fetched={fetched} skipped={skipped} failed={failed} "
          f"cached={len(cache)}")
    if depth:
        print(f"bars per symbol: min={depth[0]} median={depth[len(depth)//2]} "
              f"max={depth[-1]}")
        print(f"cache file: {CACHE} "
              f"({os.path.getsize(CACHE)/1e6:.1f} MB)")
    return 0 if fetched or skipped else 1


if __name__ == '__main__':
    sys.exit(main())
