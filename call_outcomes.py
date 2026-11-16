"""[P-60] call_outcomes: every advisor call re-scored on each trading day for
60 days. Filled each morning after the daily-bar sync (scheduler), from stored
bars only (token-free). Never writes the official grade columns.

    python3 -m call_outcomes            # through the latest Nifty bar
    python3 -m call_outcomes --all      # backfill every call since BARS_START
"""
import sys
from datetime import date, datetime

import benchmarks
import data_jobs
import database as db
import sector_view
from call_scoring import HORIZON, INDEX, direction, score_call, summarize
from universe_calls import _paged

TABLE = 'call_outcomes'
BARS_START = '2026-08-25'          # first stored daily bars
RECENT_DAYS = 10                   # stored rows read per morning: each row carries running mae/mfe,
                                   # so a call's latest row is enough; a longer gap recomputes from td 1
_KEY = ('source', 'account', 'run_date', 'symbol')


# a total order per table: paging with ties at a 1000-row boundary skips rows
_ORDER = {'portfolio_advice': ('run_date', 'symbol', 'id'), 'account_calls': ('run_date', 'account', 'symbol'),
          'universe_calls': ('run_date', 'symbol'), TABLE: ('run_date', 'source', 'account', 'symbol', 'td')}


def _rows(table: str, since: str, cols: str = '*', date_from: str = None) -> list:
    def q():
        x = db.supabase.table(table).select(cols).gte('run_date', since)
        x = x.gte('date', date_from) if date_from else x
        for c in _ORDER[table]:
            x = x.order(c)
        return x
    return _paged(q)


def key(c: dict) -> tuple:
    return tuple(str(c[k]) for k in _KEY)


def _weekday(d: str) -> str:
    return date.fromisoformat(str(d)[:10]).strftime('%a')


def load_calls(since: str) -> list:
    """Every scoreable call with run_date >= since, as score_call inputs + `attrs`."""
    uni = _rows('universe_calls', since)
    sector = {r['symbol']: r.get('sector') for r in uni}
    uni_px = {(str(r['run_date']), r['symbol']): r.get('last_price') for r in uni}
    out, seen = [], set()

    def add(source, account, row, symbol, verdict, entry, attrs, target=None, target_entry=None):
        c = {'source': source, 'account': account, 'run_date': str(row['run_date'])[:10], 'symbol': symbol,
             'entry': float(entry) if entry else None, 'direction': direction(verdict),
             'target': target, 'target_entry': float(target_entry) if target_entry else None}
        if not c['direction'] or not c['entry'] or key(c) in seen:
            return
        seen.add(key(c))
        c['attrs'] = dict(attrs, verdict=verdict, weekday=_weekday(c['run_date']),
                          sector_group=sector_view.group_of(symbol, sector.get(symbol) or ''))
        out.append(c)

    def base(row, regime):
        return {'regime': regime, 'trigger_type': row.get('trigger_type'), 'confidence': row.get('confidence'),
                'quality': row.get('quality'), 'sector_rule': row.get('sector_rule'),
                'rotation_reason': row.get('rotation_reason'), 'thesis_action': row.get('thesis_action')}

    def with_target(source, account, row, verdict, attrs, buy_px=None):
        tgt = row.get('rotation_target_symbol')
        t_px = (buy_px or uni_px.get((str(row['run_date'])[:10], tgt))) if tgt else None
        add(source, account, row, row['symbol'], verdict, row.get('last_price'), attrs, tgt, t_px)
        if tgt and t_px:
            add('target', account, row, tgt, 'BUY', t_px, attrs)

    for r in _rows('portfolio_advice', since):
        if r.get('is_official') is False:
            continue
        a = base(r, r.get('market_regime'))
        a['thesis_action'] = ((r.get('indicators') or {}).get('thesis') or {}).get('action')
        with_target('advice', 'primary', r, r['verdict'], a, r.get('rotation_buy_price'))
    for r in _rows('account_calls', since):
        a = base(r, r.get('regime'))
        with_target('natwar', r['account'], r, r.get('sector_verdict') or r['verdict'], a)
        if r.get('sector_verdict') and r['sector_verdict'] != r['verdict']:
            add('natwar_v1', r['account'], r, r['symbol'], r['verdict'], r.get('last_price'), a)
    for r in uni:
        add('universe', 'universe', r, r['symbol'], r['verdict'], r.get('last_price'), base(r, r.get('regime')))
    return out


def _bars(since: str, through: str) -> dict:
    rows = _paged(lambda: db.supabase.table('candles').select('symbol,trade_date,close').eq('interval', 'day')
                  .gte('trade_date', since).lte('trade_date', through).order('trade_date').order('symbol'))
    out = {}
    for r in rows:
        if r.get('close') is not None:
            out.setdefault(r['symbol'], {})[str(r['trade_date'])[:10]] = float(r['close'])
    return out


def _stored(since: str, date_from: str = None) -> dict:
    """call key -> stored rows (dated >= date_from when given), for calls with run_date >= since."""
    out = {}
    for r in _rows(TABLE, since, date_from=date_from):
        out.setdefault(key(r), []).append(r)
    return out


def update(through: str = None, since: str = None, rescore: bool = False) -> int:
    """`rescore` ignores stored rows and recomputes every day of every call
    since `since` (upsert overwrites) — for a column added after the fact."""
    if through is None:                # never score a bar that is still trading
        import pytz
        through = data_jobs.last_completed_trading_day(datetime.now(pytz.timezone('Asia/Kolkata')))
    nifty = _paged(lambda: db.supabase.table('candles').select('trade_date').eq('interval', 'day')
                   .eq('symbol', INDEX).gte('trade_date', since or BARS_START).lte('trade_date', through)
                   .order('trade_date'))
    days = sorted(str(r['trade_date'])[:10] for r in nifty)
    if not days:
        return 0
    if since is None:                  # open calls: fewer than HORIZON Nifty days since the call
        since = days[-(HORIZON + 1)] if len(days) > HORIZON else days[0]
    days = [d for d in days if d >= since]
    bars = _bars(since, through)
    stored = {} if rescore else _stored(since, days[-RECENT_DAYS] if len(days) >= RECENT_DAYS else None)
    sect, size, priced = benchmarks.sectors(), benchmarks.size_buckets(), list(bars)
    new, n_calls = [], 0
    for c in load_calls(since):
        have = stored.get(key(c), [])
        prior = None
        if have:
            last = max(have, key=lambda r: int(r['td']))
            prior = {'td': int(last['td']), 'date': str(last['date'])[:10],
                     'mae': min(float(r['mae_pct']) for r in have), 'mfe': max(float(r['mfe_pct']) for r in have)}
            if prior['td'] >= HORIZON:
                continue
        rows = score_call(c, bars, days, prior,
                          cohorts=benchmarks.cohorts_for(c['symbol'], priced, sect, size))
        if rows:
            n_calls += 1
            new += rows
    for i in range(0, len(new), 500):
        db.supabase.table(TABLE).upsert(new[i:i + 500], on_conflict=','.join(_KEY) + ',td').execute()
    print(f"[call_outcomes] wrote {len(new)} rows for {n_calls} calls through {through}")
    return len(new)


def latest_summary(calls: list) -> dict:
    """call key -> call_scoring.summarize over its stored rows (calls with none are absent)."""
    if not calls:
        return {}
    stored = _stored(min(c['run_date'] for c in calls))
    return {key(c): summarize(stored[key(c)], c['direction']) for c in calls if key(c) in stored}


if __name__ == '__main__':
    args = sys.argv[1:]
    update(None, BARS_START if '--all' in args else None, rescore='--all' in args)
