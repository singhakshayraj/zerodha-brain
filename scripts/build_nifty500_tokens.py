"""One-off/maintenance: build data/nifty500.csv (symbol,token,sector,industry).

Joins the NSE Nifty 500 constituent list against Kite's PUBLIC instrument
master — the authenticated OMS has no instruments endpoint (kite_client.py
stubs it dead), so tokens must be pinned in-repo, refreshed manually after
quarterly index reconstitution. Run:

    python3 scripts/build_nifty500_tokens.py <ind_nifty500list.csv> <kite_NSE.csv>

Sources (both public, no auth):
  https://niftyindices.com/IndexConstituent/ind_nifty500list.csv
  https://api.kite.trade/instruments/NSE
"""
import csv
import os
import sys

OUT_PATH = os.path.join(os.path.dirname(__file__), '..', 'data', 'nifty500.csv')


def build_rows(constituents: list, instruments: list) -> tuple:
    """Pure join: constituent rows × instrument master → universe rows.
    Returns (rows, missing_symbols). Only NSE EQ-segment equity entries are
    eligible; a constituent with no token match is reported, not dropped
    silently."""
    tokens = {}
    for r in instruments:
        if (r.get('segment') == 'NSE' and r.get('instrument_type') == 'EQ'
                and r.get('tradingsymbol')):
            tokens[r['tradingsymbol']] = r['instrument_token']

    rows, missing = [], []
    for c in constituents:
        sym = (c.get('Symbol') or '').strip()
        if not sym:
            continue
        token = tokens.get(sym)
        if not token:
            missing.append(sym)
            continue
        rows.append({
            'symbol': sym,
            'instrument_token': int(token),
            'sector': (c.get('Industry') or '').strip(),
            'industry': (c.get('Industry') or '').strip(),
            'company_name': (c.get('Company Name') or '').strip(),
        })
    return rows, missing


def main():
    if len(sys.argv) != 3:
        print(__doc__)
        sys.exit(1)
    with open(sys.argv[1], newline='', encoding='utf-8-sig') as f:
        constituents = list(csv.DictReader(f))
    with open(sys.argv[2], newline='', encoding='utf-8-sig') as f:
        instruments = list(csv.DictReader(f))

    rows, missing = build_rows(constituents, instruments)
    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, 'w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=[
            'symbol', 'instrument_token', 'sector', 'industry', 'company_name'])
        w.writeheader()
        w.writerows(sorted(rows, key=lambda r: r['symbol']))

    print(f"wrote {len(rows)} rows to {os.path.normpath(OUT_PATH)}")
    if missing:
        print(f"UNMATCHED ({len(missing)}): {', '.join(missing)}")


if __name__ == '__main__':
    main()
