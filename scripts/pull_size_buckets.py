#!/usr/bin/env python3
"""Refresh data/size_buckets.csv (symbol,bucket) from the public niftyindices
constituent lists: Nifty 100 -> large, Midcap 150 -> mid, Smallcap 250 -> small.
Anything else is `micro` by default (benchmarks.size_buckets). Re-run with the
Nifty-500 pin audit. [P-68 phase 0]"""
import csv
import io
import os
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, 'data', 'size_buckets.csv')
LISTS = (('ind_nifty100list.csv', 'large'), ('ind_niftymidcap150list.csv', 'mid'),
         ('ind_niftysmallcap250list.csv', 'small'))
BASE = 'https://niftyindices.com/IndexConstituent/'


def fetch(name: str) -> str:
    req = urllib.request.Request(BASE + name, headers={'User-Agent': 'Mozilla/5.0'})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode('utf-8-sig')


def main():
    rows = []
    for name, bucket in LISTS:
        for r in csv.DictReader(io.StringIO(fetch(name))):
            if r.get('Symbol'):
                rows.append((r['Symbol'].strip(), bucket))
    with open(OUT, 'w', newline='') as fh:
        w = csv.writer(fh)
        w.writerow(['symbol', 'bucket'])
        w.writerows(sorted(rows))
    print(f"wrote {len(rows)} rows -> {OUT}")


if __name__ == '__main__':
    main()
