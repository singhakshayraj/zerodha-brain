"""Strategy-lab historical replay [P-70]: run the 54 live lab variants over
2019-2025 NSE 1-minute history (github.com/ganeshbiyer/nse_historical_data),
offline on the Mac. Same lab.py / lab_book / lab_strategies code as live; a
fake Kite serves completed 5-min bars only (no lookahead) and an in-memory
store replaces Supabase. Nothing here touches prod.

  python scripts/lab_backtest.py fetch           # download + 5-min resample (~6 GB in, ~1 GB kept)
  python scripts/lab_backtest.py run [from] [to] # replay days, writes trades.parquet
  python scripts/lab_backtest.py report          # per-variant PF / net / t / SPRT
  python scripts/lab_backtest.py exp X01 [workers]  # one registered experiment, 2018-03..2024-12

Known biases (read before believing a number): survivorship (today's ~550
Nifty 500 names only), one source with repeated sessions (deduped), corporate
actions handled by excluding a symbol 20 sessions after any >25% overnight gap.
"""
import os
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

DATA = os.path.expanduser(os.getenv('NSE_DATA', '~/Desktop/GITHUB/nse_data'))
SRC = 'https://raw.githubusercontent.com/ganeshbiyer/nse_historical_data/main/{}.parquet'
LIST = 'https://api.github.com/repos/ganeshbiyer/nse_historical_data/contents'


def to_5min(df):
    """1-min bars (Date, Open, High, Low, Close, Volume) -> 09:15-anchored 5-min."""
    df = df.drop_duplicates('Date', keep='last').set_index('Date').sort_index()
    df = df.between_time('09:15', '15:29')
    out = df.resample('5min').agg({'Open': 'first', 'High': 'max', 'Low': 'min',
                                   'Close': 'last', 'Volume': 'sum'})
    return out.dropna(subset=['Open']).reset_index()


def _get(url, path=None, tries=6):
    for i in range(tries):
        try:
            with urllib.request.urlopen(url, timeout=120) as r:
                data = r.read()
            if path is None:
                return data
            with open(path, 'wb') as f:
                f.write(data)
            return path
        except Exception as e:
            if i == tries - 1:
                raise
            time.sleep(2 ** i)


def _one(sym):
    import pandas as pd
    out = os.path.join(DATA, '5m', f'{sym}.parquet')
    if os.path.exists(out):
        return sym, 'have'
    raw = os.path.join(DATA, 'raw', f'{sym}.parquet')
    _get(SRC.format(urllib.request.quote(sym)), raw)
    df = pd.read_parquet(raw, columns=['Date', 'Open', 'High', 'Low', 'Close', 'Volume'])
    to_5min(df).to_parquet(out + '.tmp', index=False)
    os.replace(out + '.tmp', out)
    os.remove(raw)                      # disk is tight; the source can be re-pulled
    return sym, 'ok'


def fetch(workers=4):
    import json
    os.makedirs(os.path.join(DATA, 'raw'), exist_ok=True)
    os.makedirs(os.path.join(DATA, '5m'), exist_ok=True)
    names = [x['name'][:-8] for x in json.loads(_get(LIST)) if x['name'].endswith('.parquet')]
    pri = os.path.join(DATA, 'priority.txt')      # most liquid first, so a run can start early
    rank = {s: i for i, s in enumerate(open(pri).read().split())} if os.path.exists(pri) else {}
    names.sort(key=lambda s: rank.get(s, len(rank)))
    done = 0
    with ThreadPoolExecutor(workers) as ex:
        for sym, st in ex.map(lambda s: _safe(s), names):
            done += 1
            print(f'[fetch] {done}/{len(names)} {sym} {st}', flush=True)


def _safe(sym):
    try:
        return _one(sym)
    except Exception as e:
        return sym, f'FAIL {e}'


# ── replay ──────────────────────────────────────────────────────────────────
NIFTY = 'NIFTY50-INDEX'
GAP_EXCLUDE, EXCLUDE_SESSIONS, ADV_DAYS = 0.25, 20, 20


def _p(name):
    return os.path.join(DATA, name)


def daily_panel():
    """date, symbol, open, close, volume per session (cached)."""
    import pandas as pd
    out = _p('daily.parquet')
    if os.path.exists(out):
        return pd.read_parquet(out)
    rows = []
    for f in sorted(os.listdir(_p('5m'))):
        if not f.endswith('.parquet'):
            continue
        d = pd.read_parquet(os.path.join(_p('5m'), f))
        g = d.groupby(d['Date'].dt.strftime('%Y-%m-%d'))
        rows.append(pd.DataFrame({'open': g['Open'].first(), 'close': g['Close'].last(),
                                  'volume': g['Volume'].sum()}).reset_index(names='date')
                    .assign(symbol=f[:-8]))
    panel = pd.concat(rows, ignore_index=True)
    panel.to_parquet(out, index=False)
    return panel


def universes(panel, n=200):
    """{date: [(symbol, symbol, rank)]}: top n by prior-20-session mean traded
    value (same rule as lab_db.adv), point in time. A symbol is out for the
    day of any >25% overnight gap and the 20 sessions after (corporate action
    or data break: its 15-day lookback straddles it)."""
    p = panel[~panel['symbol'].str.endswith('-INDEX')].sort_values(['symbol', 'date']).copy()
    g = p.groupby('symbol', sort=False)
    p['adv'] = (p['close'] * p['volume']).groupby(p['symbol'], sort=False).transform(
        lambda s: s.rolling(ADV_DAYS).mean().shift(1))
    bad = ((p['open'] / g['close'].shift(1)) - 1).abs() > GAP_EXCLUDE
    p['excl'] = bad.astype(int).groupby(p['symbol'], sort=False).transform(
        lambda s: s.rolling(EXCLUDE_SESSIONS + 1, min_periods=1).max()).astype(bool)
    p = p[p['adv'].notna() & ~p['excl']]
    out = {}
    for d, grp in p.groupby('date'):
        top = grp.nlargest(n, 'adv')['symbol'].tolist()
        out[d] = [(s, s, i + 1) for i, s in enumerate(top)]
    return out


class HistKite:
    """Kite stand-in over history: only bars that have fully closed by to_date
    (live returns the in-progress bar too; dropping it is the conservative
    side). Never offers an order endpoint."""
    def __init__(self, bars):
        self.bars = bars            # token -> (starts: list[str 'YYYY-MM-DD HH:MM'], rows)

    def get_historical_data(self, token, interval, from_date, to_date):
        import bisect
        from datetime import datetime, timedelta
        starts, rows = self.bars.get(token, ([], []))
        last = (datetime.strptime(to_date, '%Y-%m-%d %H:%M:%S') - timedelta(minutes=5)).strftime('%Y-%m-%d %H:%M')
        return rows[bisect.bisect_left(starts, from_date[:16]):bisect.bisect_right(starts, last)]


class Clock:
    def __init__(self, day, hms='09:14:00'):
        from datetime import datetime
        import pytz
        self.t = pytz.timezone('Asia/Kolkata').localize(datetime.strptime(f'{day} {hms}', '%Y-%m-%d %H:%M:%S'))

    def __call__(self):
        return self.t

    def sleep(self, s):
        from datetime import timedelta
        self.t += timedelta(seconds=s)


def _bars_for(df, day):
    """15 calendar days back to `day` -> (starts, Kite-shaped bar dicts)."""
    import pandas as pd
    lo = pd.Timestamp(day) - pd.Timedelta(days=15)
    hi = pd.Timestamp(day) + pd.Timedelta(days=1)
    w = df[(df['Date'] >= lo) & (df['Date'] < hi)]
    starts = w['Date'].dt.strftime('%Y-%m-%d %H:%M').tolist()
    rows = [{'timestamp': s.replace(' ', 'T') + ':00+0530', 'open': o, 'high': h, 'low': l,
             'close': c, 'volume': int(v)}
            for s, o, h, l, c, v in zip(starts, w['Open'], w['High'], w['Low'], w['Close'], w['Volume'])]
    return starts, rows


def replay_days(days, uni, exp=None):
    """Run lab.LabRunner over each day with in-memory storage; returns closed trades."""
    import pandas as pd
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    import config
    config._tunable_overrides = lambda: {}          # prod has none; never read prod
    config.LAB_ENABLED = False
    import lab
    import lab_db
    from tests.fake_supabase import FakeSupabase
    if exp:
        import lab_experiments
        lab.VARIANTS = lab_experiments.EXPERIMENTS[exp]
    real_stdout, sys.stdout = sys.stdout, open(os.devnull, 'w')   # signal_engine logs
    lo = (pd.Timestamp(days[0]) - pd.Timedelta(days=16))
    hi = pd.Timestamp(days[-1]) + pd.Timedelta(days=1)   # only this chunk's bars: was ~2 GB/worker
    frames = {}
    for s in {x[0] for d in days for x in uni.get(d, [])} | {NIFTY}:
        df = pd.read_parquet(os.path.join(_p('5m'), f'{s}.parquet'))
        frames[s] = df[(df['Date'] >= lo) & (df['Date'] < hi)]
    out = []
    try:
        for d in days:
            u = uni.get(d)
            if not u:
                continue
            bars = {s: _bars_for(frames[s], d) for s, _, _ in u}
            bars[lab.NIFTY_TOKEN] = _bars_for(frames[NIFTY], d)
            lab_db.sb = FakeSupabase()
            clock = Clock(d)
            t0 = time.time()
            lab.LabRunner(HistKite(bars), d, lambda: False, clock=clock, universe=u,
                          sleep=clock.sleep).run()
            got = [r for r in lab_db.sb.tables.get('lab_trades', []) if r.get('status') == 'CLOSED']
            out += got
            print(f'[replay] {d} trades={len(got)} {time.time() - t0:.1f}s', file=sys.stderr, flush=True)
    finally:
        sys.stdout = real_stdout
    return out


HOLDOUT = '2025-01-01'   # experiments never see 2025 (LAB_EXPERIMENTS.md)


def run(start='2018-03-01', end='2025-12-31', workers=6, exp=None):
    import pandas as pd
    from concurrent.futures import ProcessPoolExecutor, as_completed
    uni = universes(daily_panel())
    days = sorted(d for d in uni if start <= d <= end)
    assert not (exp and end >= HOLDOUT), 'experiments stop at 2024-12-31; 2025 is the locked holdout'
    pre = f'bt/exp_{exp}_' if exp else 'bt/trades_'
    os.makedirs(_p('bt'), exist_ok=True)
    chunks = {}
    for d in days:                                  # one chunk per quarter: keeps P+E cores busy
        chunks.setdefault(f'{d[:4]}q{(int(d[5:7]) - 1) // 3 + 1}', []).append(d)
    todo = [(y, ds) for y, ds in chunks.items() if not os.path.exists(_p(f'{pre}{y}.parquet'))]
    with ProcessPoolExecutor(workers) as ex:
        futs = {ex.submit(replay_days, ds, {d: uni[d] for d in ds}, exp): y for y, ds in todo}
        for f in as_completed(futs):
            y = futs[f]
            pd.DataFrame(f.result()).to_parquet(_p(f'{pre}{y}.parquet'), index=False)
            print(f'[run] {y} done', flush=True)


BLOCKS = [('2018-03-01', '2020-12-31'), ('2021-01-01', '2022-12-31'), ('2023-01-01', '2025-12-31')]


def verdicts(trades, n_days, variants=None):
    """Pre-registered history rule (spec §5, 2026-11-18): full-window SPRT
    promoted + t_day >= 3.72 + PF >= 1.3 + net >= Rs100 per replay day, and
    net > 0 in every block. `trades`: closed lab_trades rows (dicts)."""
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from lab_stats import cumulative, day_row, PROMOTE_INR_DAY
    from lab_strategies import VARIANTS
    VARIANTS = variants or VARIANTS
    by = {}
    for t in trades:
        by.setdefault(t['strategy_id'], {}).setdefault(t['trade_date'], []).append(t)
    out = []
    for v in VARIANTS:
        days = by.get(v.id, {})
        c = cumulative([day_row(days[d]) for d in sorted(days)], len(VARIANTS))
        per_day = c['cum_net_inr'] / n_days if n_days else 0.0
        blocks = [round(sum(t['net_inr'] for d in days if a <= d <= b for t in days[d]))
                  for a, b in BLOCKS]
        ok = (c['sprt_state'] == 'promoted' and per_day >= PROMOTE_INR_DAY
              and all(x > 0 for x in blocks))
        out.append({'id': v.id, 'n': c['cum_n'], 'days': c['cum_days'], 'net': round(c['cum_net_inr']),
                    'per_day': round(per_day, 1), 'pf': c['pf'] and round(c['pf'], 2),
                    'net_r': c['cum_n'] and round(c['cum_net_r'] / c['cum_n'], 3),
                    't': c['t_day'] and round(c['t_day'], 2), 'sprt': c['sprt_state'],
                    'blocks': blocks, 'pass': ok})
    return sorted(out, key=lambda r: -(r['pf'] or 0))


def report(exp=None, parent=None):
    import glob
    import pandas as pd
    t = pd.concat([pd.read_parquet(f) for f in sorted(glob.glob(_p(f'bt/exp_{exp}_*.parquet' if exp else 'bt/trades_*.parquet')))])
    if exp:   # same explore window for the parent, from the frozen 54-variant run
        base = pd.concat([pd.read_parquet(f) for f in glob.glob(_p('bt/trades_*.parquet'))])
        t = pd.concat([t, base[(base['strategy_id'] == parent) & (base['trade_date'] < HOLDOUT)]])
    days = set(universes(daily_panel()))
    n_days = len({d for d in days if d < HOLDOUT} if exp else days & set(t['trade_date']))
    variants = None
    if exp:
        import lab_experiments
        from lab_strategies import VARIANTS as live
        variants = lab_experiments.EXPERIMENTS[exp] + [v for v in live if v.id == parent]
    rows = verdicts(t.to_dict('records'), n_days, variants)
    for r in rows:   # raw edge before costs: what the change bought
        g = t[t['strategy_id'] == r['id']]['gross_inr']
        r['gross_pf'] = round(g[g > 0].sum() / -g[g < 0].sum(), 2) if (g < 0).any() else None
    print(f'{len(t)} trades over {n_days} replay days')
    print(pd.DataFrame(rows).to_string(index=False))
    pd.DataFrame(rows).to_csv(_p(f'bt/verdicts_{exp}.csv' if exp else 'bt/verdicts.csv'), index=False)


if __name__ == '__main__':
    cmd = sys.argv[1] if len(sys.argv) > 1 else ''
    if cmd == 'fetch':
        fetch()
    elif cmd == 'run':
        run(*sys.argv[2:4], *map(int, sys.argv[4:5]))
    elif cmd == 'report':
        report()
    elif cmd == 'exp':
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        run('2018-03-01', '2024-12-31', int(sys.argv[3]) if len(sys.argv) > 3 else 6, sys.argv[2])
        import lab_experiments
        report(sys.argv[2], lab_experiments.PARENT[sys.argv[2]])
    else:
        print(__doc__)
