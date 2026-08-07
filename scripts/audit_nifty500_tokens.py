"""Maintenance: check data/nifty500.csv's pinned tokens against Kite's LIVE
public instrument master. Read-only — reports, never rewrites.

    python3 scripts/audit_nifty500_tokens.py

Why this exists. The authenticated OMS has no instruments endpoint
(kite_client.py stubs it dead), so Nifty-500 tokens are pinned in-repo and go
stale silently: a delisted or renamed name keeps its dead token, and the
advisor's daily rotation scan 400s on it with `invalid token` — one line buried
in thousands. That is finding [C2] (JBCHEPHARM, 2026-08-07). A single log line
is easy to miss; this turns "is the pin still good?" into one command.

Run it after quarterly index reconstitution, or whenever an `invalid token`
shows up in the advisor logs. If it reports anything, fix with
`scripts/build_nifty500_tokens.py` (full rebuild) or by hand for a single name.

No auth needed — https://api.kite.trade/instruments/NSE is public.

Exit codes: 0 = clean, 1 = discrepancies found (so CI/cron can gate on it).
"""
import csv
import io
import os
import sys
import urllib.request

INSTRUMENTS_URL = 'https://api.kite.trade/instruments/NSE'
PINNED_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           '..', 'data', 'nifty500.csv')


def live_equity_tokens(text: str) -> dict:
    """tradingsymbol -> instrument_token for NSE EQ-segment equities only.
    Same filter as build_nifty500_tokens.build_rows, deliberately — an audit
    that filtered differently from the builder would report phantom drift."""
    out = {}
    for r in csv.DictReader(io.StringIO(text)):
        if (r.get('segment') == 'NSE' and r.get('instrument_type') == 'EQ'
                and r.get('tradingsymbol')):
            out[r['tradingsymbol']] = r['instrument_token']
    return out


def audit(pinned: list, live: dict) -> tuple:
    """Pure: (wrong_token, absent, ok_count). `wrong_token` is a live rename or
    re-issue; `absent` is delisted/renamed away — both make the scan fail, but
    only the first has an obvious repair."""
    wrong, absent, ok = [], [], 0
    for r in pinned:
        sym, tok = r.get('symbol'), r.get('instrument_token')
        if sym not in live:
            absent.append((sym, tok, r.get('company_name', '')))
        elif live[sym] != tok:
            wrong.append((sym, tok, live[sym], r.get('company_name', '')))
        else:
            ok += 1
    return wrong, absent, ok


def main() -> int:
    with open(PINNED_PATH, newline='') as f:
        pinned = list(csv.DictReader(f))
    print(f"[audit] pinned rows: {len(pinned)}")

    print(f"[audit] fetching {INSTRUMENTS_URL} ...")
    with urllib.request.urlopen(INSTRUMENTS_URL, timeout=120) as resp:
        live = live_equity_tokens(resp.read().decode('utf-8', 'replace'))
    print(f"[audit] live NSE EQ symbols: {len(live)}")
    if len(live) < 1000:
        # A truncated or error response would otherwise read as "everything is
        # delisted" and send someone rebuilding a perfectly good file.
        print("[audit] ABORT: instrument master looks truncated; not judging the pin")
        return 2

    wrong, absent, ok = audit(pinned, live)
    print(f"[audit] token matches live : {ok}")
    print(f"[audit] WRONG token        : {len(wrong)}")
    print(f"[audit] absent from master : {len(absent)}")

    for sym, old, new, name in wrong:
        print(f"  WRONG   {sym:16} {old} -> {new}   {name}")
    for sym, tok, name in absent:
        print(f"  ABSENT  {sym:16} {tok} (delisted/renamed)   {name}")

    if wrong or absent:
        print("\n[audit] FAIL — fix via scripts/build_nifty500_tokens.py, "
              "or drop the row if the name is genuinely gone.")
        return 1
    print("\n[audit] OK — every pinned token matches the live master.")
    return 0


if __name__ == '__main__':
    sys.exit(main())
