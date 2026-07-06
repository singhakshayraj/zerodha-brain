#!/usr/bin/env python3
"""Nightly level-pack builder (ENGINEERING_SPEC M3). Runs on the Mac, writes
Supabase. Cron ~07:00 IST:

    0 7 * * 1-5  cd /path/zerodha-brain && python3 scripts/build_level_pack.py

Fetches daily candles per universe symbol via the real KiteClient and upserts
one level_pack row per symbol for today. Needs a valid enc_token in Supabase
(same one the brain uses) or QA_MODE for a synthetic dry-run.
"""
import os
import sys
from datetime import datetime

import pytz

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
import database as db
import level_pack
from market_data import MarketData

IST = pytz.timezone('Asia/Kolkata')


def _make_kite():
    if config.QA_MODE:
        from qa_market import FakeKiteClient
        return FakeKiteClient()
    from kite_client import KiteClient
    token = db.get_enc_token()
    if not token:
        raise SystemExit("No enc_token in Supabase — cannot fetch candles")
    return KiteClient(token)


def main():
    today = datetime.now(IST).strftime('%Y-%m-%d')
    kite = _make_kite()
    md = MarketData(kite)

    symbols = list(config.NIFTY50_INSTRUMENT_TOKENS.items())
    ok = 0
    for sym, token in symbols:
        try:
            md._instrument_cache[sym] = token
            # 60-day daily-ish history via 60-minute candles collapsed to days
            candles = md.get_candles(sym, '60minute', days=60)
            daily = level_pack.daily_ohlc(candles)
            if not daily:
                print(f"[build_level_pack] no data for {sym}, skipping")
                continue
            row = level_pack.build(sym, today, daily)
            db.upsert_level_pack(row)
            ok += 1
        except Exception as e:
            print(f"[build_level_pack] {sym} failed: {e}")
    print(f"[build_level_pack] done — {ok}/{len(symbols)} symbols for {today}")


if __name__ == '__main__':
    main()
