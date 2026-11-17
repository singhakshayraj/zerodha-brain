"""Point-in-time Nifty 500 membership [P-68 phase 0].

Every backtest so far applied today's pin to the past (survivorship). This
module answers "who was in the Nifty 500 on date D" from
data/universe_history.csv (symbol, joined, left, status), built by
scripts/build_universe_history.py from NSE Indices' semi-annual press
releases. `joined == ''` = in the index since before our data (2021-06);
`left == ''` = still in. Pure.
"""
import csv
import os
import re
from datetime import date

ROOT = os.path.dirname(os.path.abspath(__file__))
PATH = os.path.join(ROOT, 'data', 'universe_history.csv')

_HEADING = re.compile(r'^(?:[0-9a-z]{1,2}\)\s*)?(Nifty ?[A-Za-z0-9 &:/-]+?)\s*$')   # '2) Nifty 500', 'c) Nifty 500', 'Nifty500 Shariah'
_ROW = re.compile(r'^\d+\s+.+?\s+([A-Z0-9&-]+)\s*$')
_EFFECTIVE = re.compile(r'effective\s+(?:from\s+)?([A-Z][a-z]+ \d{1,2}, \d{4})')
_MONTHS = {m: i for i, m in enumerate(
    ['January', 'February', 'March', 'April', 'May', 'June', 'July', 'August',
     'September', 'October', 'November', 'December'], 1)}


def _default_effective(released: str) -> str:
    """Nifty 500 reviews take effect on the last day of March / September."""
    y, m = int(released[:4]), int(released[5:7])
    return f'{y}-03-31' if m <= 6 else f'{y}-09-30'


def parse_release(text: str, released: str) -> dict:
    """{'effective', 'excluded': [symbols], 'included': [symbols]} for the
    Nifty 500 section of one press release; empty lists when absent."""
    lines = [l.strip() for l in text.splitlines()]
    out = {'effective': None, 'excluded': [], 'included': []}
    m = _EFFECTIVE.search(text)
    if m:
        mon, d, y = m.group(1).replace(',', '').split()
        out['effective'] = date(int(y), _MONTHS[mon], int(d)).isoformat()
    else:
        out['effective'] = _default_effective(released)
    section, bucket = False, None
    for l in lines:
        h = _HEADING.match(l)
        if h:
            if section:
                break                      # next index's section
            section = h.group(1) == 'Nifty 500'
            continue
        if not section:
            continue
        if 'being excluded' in l:
            bucket = 'excluded'
        elif 'being included' in l:
            bucket = 'included'
        elif bucket:
            r = _ROW.match(l)
            if r and not l.startswith('Sr.'):
                out[bucket].append(r.group(1))
    return out


def build_rows(pin: set, events: list) -> list:
    """One row per membership spell: [{symbol, joined, left}]. A name that
    leaves and later rejoins gets two rows, so members() sees both spells.
    Events: [{effective, included, excluded}]. joined == '' = member since
    before our data; left == '' = still in."""
    spells = {s: [{'symbol': s, 'joined': '', 'left': ''}] for s in pin}

    def open_spell(s):
        return next((r for r in spells.get(s, []) if r['left'] == ''), None)

    for ev in sorted(events, key=lambda e: e['effective']):
        d = ev['effective']
        for s in ev['excluded']:
            cur = open_spell(s)
            if cur is None:
                spells.setdefault(s, []).append({'symbol': s, 'joined': '', 'left': d})
            else:
                cur['left'] = d
        for s in ev['included']:
            cur = open_spell(s)
            if cur is None:
                spells.setdefault(s, []).append({'symbol': s, 'joined': d, 'left': ''})
            elif cur['joined'] == '':
                cur['joined'] = d                 # the seed (or unknown-start) spell began here
    out = [r for rs in spells.values() for r in rs]
    return sorted(out, key=lambda r: (r['symbol'], r['joined']))


def rows(path: str = PATH) -> list:
    if not os.path.exists(path):
        return []
    with open(path) as fh:
        return list(csv.DictReader(fh))


def members(day: str, path: str = PATH) -> set:
    return {r['symbol'] for r in rows(path)
            if (not r['joined'] or r['joined'] <= day) and (not r['left'] or r['left'] > day)}
