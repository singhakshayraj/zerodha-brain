"""Universe calls — the daily Nifty-500 scan's verdicts, kept (shadow).

The scan in portfolio_advisor.score_universe runs advise() on ~480 names a
day but only the latest score survived in stock_universe. This module logs
every name's verdict per run_date into `universe_calls` and grades it later
with the SAME rules as the holdings grader (advisor_backtest: market-neutral
label, 10d MICRO / 30d MACRO horizons), token-free from the stored daily
bars (`candles` interval='day', kept current by data_jobs.sync_daily_bars).

Outside the n = 400 holdings read; ledger trial 259. Non-fatal by contract:
every caller wraps these in try/except.
"""
from datetime import datetime

import pytz

import advisor_backtest as bt
import database as db

IST = pytz.timezone('Asia/Kolkata')
TABLE = 'universe_calls'
INDEX = 'NIFTY 50'
_IND_KEYS = ('rsi_14', 'ema_50', 'ema_200', 'adx', 'atr_14', 'support',
             'resistance', 'daily_bars', 'relative_strength_vs_nifty',
             'trend_consistency_pct', 'mom_12_1', 'volume_trend_ratio',
             'weekly_trend', 'daily_weekly_alignment', 'overextended')


def rows_from_scan(scored: dict, run_date: str, regime: str = None,
                   quality_by_symbol: dict = None) -> list:
    """Pure: score_universe() output -> universe_calls rows. Names without a
    full advise() detail are skipped (no verdict to grade)."""
    q = quality_by_symbol or {}
    out = []
    for sym, s in (scored or {}).items():
        d = s.get('detail') or {}
        if not d.get('verdict'):
            continue
        ind = d.get('indicators') or {}
        out.append({
            'run_date': run_date, 'symbol': sym, 'sector': s.get('sector'),
            'verdict': d['verdict'], 'trigger_type': d.get('trigger_type'),
            'trend_score': d.get('trend_score', s.get('score')),
            'confidence': d.get('confidence'), 'regime': regime,
            'weekly': s.get('weekly'),
            'last_price': d.get('last_price') or s.get('last_close'),
            'quality': q.get(sym),
            'indicators': {k: ind[k] for k in _IND_KEYS if ind.get(k) is not None},
        })
    return out


def log_scan(scored: dict, run_date: str, regime: str = None) -> int:
    """Persist today's scan. Idempotent on (run_date, symbol)."""
    quality = {}
    try:
        import fundamentals
        import thesis
        sector = {s: v.get('sector') for s, v in (scored or {}).items()}
        store = (fundamentals.load_store() or {}).get('by_symbol') or {}
        quality = {s: thesis.quality_score(store.get(s), sector.get(s))
                   for s in scored or {}}
    except Exception as e:
        print(f"[universe_calls] quality skipped (non-fatal): {e}")
    rows = rows_from_scan(scored, run_date, regime, quality)
    if not rows:
        return 0
    try:                                    # trial 261 shadow variant
        import sector_tilt
        sector_of = {r['symbol']: r.get('sector') for r in rows}
        sector_tilt.apply(rows, sector_tilt.sector_trends(rows, sector_of), sector_of)
    except Exception as e:
        print(f"[universe_calls] sector tilt skipped (non-fatal): {e}")
    for i in range(0, len(rows), 500):
        db.supabase.table(TABLE).upsert(rows[i:i + 500],
                                        on_conflict='run_date,symbol').execute()
    return len(rows)


def _paged(q_fn) -> list:
    out, off = [], 0
    while True:
        page = q_fn().range(off, off + 999).execute().data or []
        out += page
        if len(page) < 1000:
            return out
        off += 1000


def _bars_since(min_date: str) -> dict:
    """{symbol: [{'timestamp', 'close'}...]} ascending, from stored daily bars."""
    rows = _paged(lambda: db.supabase.table('candles')
                  .select('symbol,trade_date,close').eq('interval', 'day')
                  .gte('trade_date', min_date).order('trade_date'))
    out = {}
    for r in rows:
        if r.get('close') is not None:
            out.setdefault(r['symbol'], []).append(
                {'timestamp': str(r['trade_date']), 'close': float(r['close'])})
    return out


def grade_rows(queue: list, bars: dict) -> list:
    """Pure: outcome-filled copies of the due rows (same rules as the holdings
    grader). Rows short of their horizon are left out."""
    nifty = bars.get(INDEX, [])
    now = datetime.now(IST).isoformat()
    done = []
    for row in queue:
        base = float(row.get('last_price') or 0)
        if row.get('verdict') == 'INSUFFICIENT' or not base:
            done.append({**row, 'outcome_return_pct': None,
                         'outcome_vs_nifty_pct': None,
                         'outcome_correct_alpha': None,
                         'sector_outcome_correct_alpha': None,
                         'evaluation_horizon_days': None, 'evaluated_at': now})
            continue
        horizon = bt.horizon_for(row)
        fwd_bars = bt._bars_from(bars.get(row['symbol'], []), str(row['run_date']))
        if len(fwd_bars) < horizon:
            continue
        end = fwd_bars[horizon - 1]
        fwd = (end['close'] - base) / base * 100
        nret = bt._index_return_between(nifty, str(row['run_date']),
                                        str(end['timestamp'])[:10])
        o = bt.evaluate_verdict(row, fwd, nret)
        sv = row.get('sector_verdict')
        so = (bt.evaluate_verdict({'verdict': sv}, fwd, nret)['outcome_correct_alpha']
              if sv else None)
        done.append({**row, 'sector_outcome_correct_alpha': so,
                     'outcome_return_pct': o['outcome_return_pct'],
                     'outcome_vs_nifty_pct': o['outcome_vs_nifty_pct'],
                     'outcome_correct_alpha': o['outcome_correct_alpha'],
                     'evaluation_horizon_days': horizon, 'evaluated_at': now})
    return done


def grade_due(today: str = None, table: str = TABLE,
              conflict: str = 'run_date,symbol') -> int:
    """Grade every matured, ungraded call in `table` (universe_calls, or
    account_calls for the second account). Token-free. Returns rows graded."""
    today = today or datetime.now(IST).date().isoformat()
    queue = _paged(lambda: db.supabase.table(table).select('*')
                   .is_('evaluated_at', 'null').lt('run_date', today)
                   .order('run_date'))
    if not queue:
        return 0
    bars = _bars_since(str(min(r['run_date'] for r in queue)))
    done = grade_rows(queue, bars)
    for i in range(0, len(done), 500):
        db.supabase.table(table).upsert(done[i:i + 500],
                                        on_conflict=conflict).execute()
    print(f"[{table}] graded {len(done)}/{len(queue)} due "
          f"(bars for {len(bars)} symbols)")
    return len(done)
