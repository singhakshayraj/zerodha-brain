#!/usr/bin/env python3
"""Weekly stock-profile builder (ENGINEERING_SPEC M3) — manual/standalone
entry point. The scheduler now runs this automatically on the first advisor
run of each ISO week (data_jobs.maybe_weekly_profiles); this script remains
for on-demand rebuilds:

    python3 scripts/build_profiles.py

Needs a valid enc_token in Supabase, or QA_MODE for a synthetic dry-run.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
import data_jobs
import database as db
from market_data import MarketData


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
    n = data_jobs.build_weekly_profiles(MarketData(_make_kite()))
    print(f"[build_profiles] done — {n} profiles written")


if __name__ == '__main__':
    main()
