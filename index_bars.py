"""Daily OHLC + P/E, P/B, yield of every NSE index from the public archive
file ind_close_all_<ddmmyyyy>.csv [P-68 phase 4]. Plain GET, no session;
a non-trading day is a 404 and is skipped. Watermark in app_config.
"""
import csv
import io
import urllib.error
import urllib.request
from datetime import date, timedelta

import database as db

TABLE = 'index_bars'
KEY = 'index_bars_through'
START = '2021-06-14'                       # same first day as daily_history.pkl
BASE = 'https://nsearchives.nseindia.com/content/indices/ind_close_all_'
_COLS = {'Open Index Value': 'open', 'High Index Value': 'high', 'Low Index Value': 'low',
         'Closing Index Value': 'close', 'P/E': 'pe', 'P/B': 'pb', 'Div Yield': 'div_yield'}


def url(day: str) -> str:
    y, m, d = day.split('-')
    return f'{BASE}{d}{m}{y}.csv'


def fetch(day: str):
    """CSV text for the day, or None on 404 / holiday."""
    req = urllib.request.Request(url(day), headers={'User-Agent': 'Mozilla/5.0'})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            text = r.read().decode('utf-8-sig', 'replace')
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise
    return text if text.lstrip().startswith('Index Name') else None


def _num(s):
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def parse(text: str) -> list:
    out = []
    for r in csv.DictReader(io.StringIO(text)):
        d = (r.get('Index Date') or '').strip()
        if not r.get('Index Name') or len(d) != 10:
            continue
        row = {'index_name': r['Index Name'].strip(), 'trade_date': f'{d[6:]}-{d[3:5]}-{d[:2]}'}
        row.update({k: _num(r.get(src)) for src, k in _COLS.items()})
        out.append(row)
    return out


def sync(through: str = None) -> int:
    """Fetch every weekday after the watermark up to `through` (default
    yesterday); upsert; advance the watermark to the last day served."""
    through = through or (date.today() - timedelta(days=1)).isoformat()
    last = db.get_config(KEY) or START
    d, n, served = date.fromisoformat(last) + timedelta(days=1), 0, last
    while d.isoformat() <= through:
        day = d.isoformat()
        d += timedelta(days=1)
        if date.fromisoformat(day).weekday() >= 5:
            continue
        text = fetch(day)
        if not text:
            continue
        rows = parse(text)
        for i in range(0, len(rows), 500):
            db.supabase.table(TABLE).upsert(rows[i:i + 500], on_conflict='index_name,trade_date').execute()
        n += len(rows)
        served = day
    if served != last:
        db.write_config(KEY, served)
    print(f"[index_bars] {last} -> {served}: {n} rows")
    return n


if __name__ == '__main__':
    import sys
    if '--from' in sys.argv:
        db.write_config(KEY, sys.argv[sys.argv.index('--from') + 1])
    sync()
