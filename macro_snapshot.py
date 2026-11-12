"""Daily macro / tape context, stored per trading day (`macro_daily`).

Context only: nothing here enters a verdict. The point is that every call we
log (holdings, universe, second account) can later be joined to the tape it
was made in — Nifty / Bank / mid / small-cap moves, India VIX, USDINR, crude,
and breadth of our own scan — so "do calls behave differently when FIIs are
buying and crude is falling?" becomes a testable question in ~60 days.

Sources: Kite daily bars on the retail token (indices, India VIX, the
front-month USDINR future on CDS, the front-month crude future on MCX;
futures carry ~2-4 months of history only). Breadth comes from the day's
universe_calls. FII/DII flows have no clean free source yet — left null.
"""
import csv
import io
import json
import re
import urllib.request
from datetime import datetime

import pytz

import database as db

IST = pytz.timezone('Asia/Kolkata')
INDICES = {'nifty': 256265, 'banknifty': 260105, 'midcap100': 256777,
           'smallcap100': 267017, 'vix': 264969}
FUTURES = {'usdinr': ('CDS', 'USDINR'), 'crude': ('MCX', 'CRUDEOIL')}
TOKENS_KEY = 'macro_front_tokens'
INSTRUMENTS_URL = 'https://api.kite.trade/instruments'


def front_month_tokens(today: str) -> dict:
    """{name: token} for the nearest unexpired USDINR / crude future; cached
    per expiry so the 10 MB instruments dump is read at most once a month."""
    cached = json.loads(db.get_config(TOKENS_KEY) or '{}')
    if all(cached.get(k, {}).get('expiry', '') >= today for k in FUTURES):
        return {k: cached[k]['token'] for k in FUTURES}
    raw = urllib.request.urlopen(INSTRUMENTS_URL, timeout=60).read().decode()
    best = {}
    for r in csv.DictReader(io.StringIO(raw)):
        for k, (exch, name) in FUTURES.items():
            if (r['exchange'] == exch and r['name'] == name
                    and r['instrument_type'] == 'FUT' and r['expiry'] >= today
                    and re.fullmatch(rf'{name}\d\d[A-Z]{{3}}FUT', r['tradingsymbol'])):
                # monthly contracts only (USDINR26NOVFUT) — the CDS weeklies
                # (USDINR26N13FUT) are thin and print off-market closes
                if k not in best or r['expiry'] < best[k]['expiry']:
                    best[k] = {'expiry': r['expiry'], 'token': int(r['instrument_token']),
                               'symbol': r['tradingsymbol']}
    db.write_config(TOKENS_KEY, json.dumps(best))
    return {k: best[k]['token'] for k in best}


def _series(market_data, name: str, token: int, n: int = 400) -> dict:
    from advisor_scoring import completed_bars
    market_data._instrument_cache[f'MACRO:{name}'] = token
    # Completed sessions only: during market hours Kite returns today's
    # forming bar, which must not be stored as the day's close.
    bars = completed_bars(market_data.get_candles(f'MACRO:{name}', 'day', n) or [])
    return {str(b['timestamp'])[:10]: float(b['close']) for b in bars if b.get('close') is not None}


def _pct(series: dict, days: list, i: int, k: int):
    if i - k < 0:
        return None
    a, b = series.get(days[i - k]), series.get(days[i])
    return round((b / a - 1) * 100, 2) if a and b else None


def build_rows(series: dict, breadth: dict = None) -> list:
    """Pure: {name: {day: close}} -> one row per Nifty day with level and
    1d/5d/20d % moves per series, VIX level + 1d change, breadth if given."""
    days = sorted(series.get('nifty') or {})
    rows = []
    for i, d in enumerate(days):
        data = {}
        for name, s in series.items():
            if d not in s:
                continue
            data[name] = {'close': s[d], 'd1': _pct(s, days, i, 1),
                          'd5': _pct(s, days, i, 5), 'd20': _pct(s, days, i, 20)}
        if 'vix' in data:
            data['vix']['chg'] = (round(data['vix']['close'] - series['vix'].get(days[i - 1], data['vix']['close']), 2)
                                  if i else None)
        if breadth and d in breadth:
            data['breadth'] = breadth[d]
        data['fii_net_cr'] = None                  # no clean source yet
        rows.append({'date': d, 'data': data})
    return rows


def merge_data(existing: dict, fresh: dict) -> dict:
    """Pure: fresh market fields over the stored row; keys only the stored
    row has (fii, dii, fut, valuation, fii_net_cr set by the importer) survive.
    fresh's None placeholders never clobber a stored value."""
    out = dict(existing or {})
    for k, v in fresh.items():
        if v is None and out.get(k) is not None:
            continue
        out[k] = v
    return out


def breadth_from_calls(run_date: str) -> dict:
    """% of scanned names with positive trend / above EMA200, from the day's
    universe_calls. {} when the scan has not run."""
    import universe_calls as uc
    rows = uc._paged(lambda: db.supabase.table(uc.TABLE)
                     .select('trend_score,last_price,indicators').eq('run_date', run_date))
    if not rows:
        return {}
    up = sum(1 for r in rows if (r.get('trend_score') or 0) > 0)
    above = [r for r in rows if (r.get('indicators') or {}).get('ema_200') and r.get('last_price')]
    n_above = sum(1 for r in above if r['last_price'] > r['indicators']['ema_200'])
    return {run_date: {'n': len(rows), 'pct_trend_up': round(up / len(rows) * 100, 1),
                       'pct_above_ema200': round(n_above / len(above) * 100, 1) if above else None}}


def run(market_data, today: str = None, backfill_days: int = 30) -> int:
    """Fetch, build, upsert the last `backfill_days` rows (idempotent). The
    first run passes a large number to seed history. Returns rows written."""
    today = today or datetime.now(IST).date().isoformat()
    series = {k: _series(market_data, k, t) for k, t in INDICES.items()}
    try:
        for k, t in front_month_tokens(today).items():
            series[k] = _series(market_data, k, t)
    except Exception as e:
        print(f"[macro] futures unavailable (non-fatal): {e}")
    rows = build_rows(series)
    for r in rows[-backfill_days:]:
        try:
            b = breadth_from_calls(r['date'])
            if b:
                r['data']['breadth'] = b[r['date']]
        except Exception:
            pass
    out = rows[-backfill_days:]
    # Merge, never replace: other jobs put fii/dii/fut/valuation on these rows.
    if out:
        have = {r['date']: r['data'] for r in (
            db.supabase.table('macro_daily').select('date,data')
            .gte('date', out[0]['date']).lte('date', out[-1]['date']).execute().data or [])}
        for r in out:
            r['data'] = merge_data(have.get(r['date']), r['data'])
    for i in range(0, len(out), 200):
        db.supabase.table('macro_daily').upsert(out[i:i + 200], on_conflict='date').execute()
    last = out[-1]['data'] if out else {}
    print(f"[macro] {len(out)} days through {out[-1]['date'] if out else '-'}: "
          f"nifty {last.get('nifty', {}).get('d1')}% vix {last.get('vix', {}).get('close')} "
          f"usdinr {last.get('usdinr', {}).get('close')} crude {last.get('crude', {}).get('close')}")
    return len(out)
