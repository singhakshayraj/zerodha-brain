"""Peer cohorts for grading a call against the right yardstick [P-68 phase 0].

Nifty 50 says whether the market moved; a sector cohort says whether the
*sector* moved; a size cohort says whether names of that size moved. All three
are needed to tell skill from a sector or size tilt. Cohorts are built from
our own bars (no external index data): equal-weight mean return of the peers
priced on both dates. Pure.
"""
import csv
import os

ROOT = os.path.dirname(os.path.abspath(__file__))
PIN = os.path.join(ROOT, 'data', 'nifty500.csv')
SIZE = os.path.join(ROOT, 'data', 'size_buckets.csv')
MIN_N = 5
DEFAULT_SIZE = 'micro'


def cohort_return(bars: dict, members: list, d0: str, d1: str, min_n: int = MIN_N):
    """Equal-weight close-to-close % of `members` priced on both days; None if
    fewer than `min_n` are."""
    rets = []
    for s in members:
        b = bars.get(s) or {}
        if b.get(d0) and b.get(d1):
            rets.append((b[d1] / b[d0] - 1) * 100)
    if len(rets) < min_n:
        return None
    return round(sum(rets) / len(rets), 4)


def sectors() -> dict:
    """symbol -> NSE macro-sector from the Nifty-500 pin."""
    with open(PIN) as fh:
        return {r['symbol']: r['sector'] for r in csv.DictReader(fh) if r.get('sector')}


def size_buckets() -> dict:
    """symbol -> large|mid|small from data/size_buckets.csv; absent = micro."""
    if not os.path.exists(SIZE):
        return {}
    with open(SIZE) as fh:
        return {r['symbol']: r['bucket'] for r in csv.DictReader(fh)}


def cohorts_for(symbol: str, priced: list, sect: dict, size: dict) -> dict:
    """{'sector': [peers], 'size': [peers]} among `priced` symbols, excluding
    the symbol itself. Unknown sector -> no sector cohort; unknown size = micro."""
    out = {'sector': [], 'size': []}
    s_sec = sect.get(symbol)
    s_sz = size.get(symbol, DEFAULT_SIZE)
    for p in priced:
        if p == symbol:
            continue
        if s_sec and sect.get(p) == s_sec:
            out['sector'].append(p)
        if size.get(p, DEFAULT_SIZE) == s_sz and (p in sect or p in size):
            out['size'].append(p)
    return out
