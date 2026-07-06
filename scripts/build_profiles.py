#!/usr/bin/env python3
"""Weekly stock-profile builder (ENGINEERING_SPEC M3). Runs on the Mac.
Cron ~Sunday 08:00 IST:

    0 8 * * 0  cd /path/zerodha-brain && python3 scripts/build_profiles.py

Computes trendiness / gap-follow / range profile per universe symbol over a
lookback window, applying the ≥30-sample universe-average fallback. Needs a
valid enc_token in Supabase, or QA_MODE for a synthetic dry-run.
"""
import os
import sys
from datetime import datetime

import pytz

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
import database as db
import level_pack
import stock_profile
from market_data import MarketData

IST = pytz.timezone('Asia/Kolkata')
LOOKBACK_DAYS = 90


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
    asof = datetime.now(IST).strftime('%Y-%m-%d')
    kite = _make_kite()
    md = MarketData(kite)

    # First pass: per-symbol dailies + provisional profiles (for the average)
    dailies = {}
    for sym, token in config.NIFTY50_INSTRUMENT_TOKENS.items():
        try:
            md._instrument_cache[sym] = token
            candles = md.get_candles(sym, '60minute', days=LOOKBACK_DAYS)
            dailies[sym] = level_pack.daily_ohlc(candles)
        except Exception as e:
            print(f"[build_profiles] {sym} fetch failed: {e}")
            dailies[sym] = []

    # Universe average over symbols that have enough history
    trends, gaps = [], []
    for sym, daily in dailies.items():
        if len(daily) >= stock_profile.MIN_SAMPLES:
            t = stock_profile.efficiency_ratio([d['close'] for d in daily])
            g = stock_profile.gap_follow_rate(daily)['rate']
            if t is not None:
                trends.append(t)
            if g is not None:
                gaps.append(g)
    universe_avg = {
        'trendiness': round(sum(trends) / len(trends), 4) if trends else None,
        'gap_follow_rate': round(sum(gaps) / len(gaps), 4) if gaps else None,
    }
    print(f"[build_profiles] universe avg: {universe_avg}")

    ok = 0
    for sym, daily in dailies.items():
        try:
            row = stock_profile.build(sym, asof, daily, LOOKBACK_DAYS,
                                      universe_avg=universe_avg)
            db.upsert_stock_profile(row)
            ok += 1
        except Exception as e:
            print(f"[build_profiles] {sym} failed: {e}")
    print(f"[build_profiles] done — {ok}/{len(dailies)} symbols asof {asof}")


if __name__ == '__main__':
    main()
