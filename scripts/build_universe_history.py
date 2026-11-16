#!/usr/bin/env python3
"""Build data/universe_history.csv (symbol,joined,left,status) from NSE
Indices' semi-annual press releases [P-68 phase 0].

    python3 scripts/build_universe_history.py [--prs ~/.cache/zerodha-brain/prs]

Each `ind_prs<ddmmyyyy>*.pdf` in the cache dir is parsed for its "Nifty 500"
exclusions/inclusions (universe_history.parse_release). Releases with no
Nifty 500 section are skipped and listed. `status` = trading when the symbol
is in Kite's public instrument master today, else delisted (renamed names
also land here — they need a hand entry). The current pin
(data/nifty500.csv) seeds the member set.
"""
import argparse
import csv
import glob
import os
import re
import sys
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'scripts'))

import universe_history as uh                      # noqa: E402
from audit_nifty500_tokens import INSTRUMENTS_URL, live_equity_tokens  # noqa: E402

DEFAULT_PRS = os.path.expanduser('~/.cache/zerodha-brain/prs')
PIN = os.path.join(ROOT, 'data', 'nifty500.csv')
_DATE = re.compile(r'ind_prs(\d{2})(\d{2})(\d{4})')


def release_date(path: str):
    m = _DATE.search(os.path.basename(path))
    return f'{m.group(3)}-{m.group(2)}-{m.group(1)}' if m else None


def pdf_text(path: str) -> str:
    from pypdf import PdfReader
    return '\n'.join((p.extract_text() or '') for p in PdfReader(path).pages)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--prs', default=DEFAULT_PRS)
    ap.add_argument('--out', default=uh.PATH)
    a = ap.parse_args()

    events, skipped = [], []
    for f in sorted(glob.glob(os.path.join(a.prs, 'ind_prs*.pdf'))):
        rel = release_date(f)
        if not rel:
            continue
        ev = uh.parse_release(pdf_text(f), released=rel)
        if not ev['excluded'] and not ev['included']:
            skipped.append(os.path.basename(f))
            continue
        events.append(ev)
        print(f"{os.path.basename(f)}: effective {ev['effective']} "
              f"-{len(ev['excluded'])} +{len(ev['included'])}")
    with open(PIN) as fh:
        pin = {r['symbol'] for r in csv.DictReader(fh)}
    rows = uh.build_rows(pin, events)

    req = urllib.request.Request(INSTRUMENTS_URL, headers={'User-Agent': 'Mozilla/5.0'})
    with urllib.request.urlopen(req, timeout=60) as r:
        live = live_equity_tokens(r.read().decode('utf-8', 'replace'))
    for row in rows:
        row['status'] = 'trading' if row['symbol'] in live else 'delisted'

    with open(a.out, 'w', newline='') as fh:
        w = csv.DictWriter(fh, fieldnames=['symbol', 'joined', 'left', 'status'])
        w.writeheader()
        w.writerows(rows)
    leavers = [r for r in rows if r['left']]
    print(f"wrote {len(rows)} rows -> {a.out}: {len(leavers)} leavers "
          f"({sum(r['status'] == 'delisted' for r in leavers)} delisted), "
          f"{len(events)} releases parsed, {len(skipped)} without a Nifty 500 section: {skipped}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
