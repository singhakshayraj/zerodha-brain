"""[P-56] Data watchdog: after the close, the VERIFY-ledger checks that can
be answered from the database alone, each with the number that proves it
and the most likely cause when it fails. Written to app_config
'watchdog_latest'; the next morning's brief carries the failures. Token-free,
display only. `python3 -m data_watchdog [YYYY-MM-DD]` runs it by hand."""
import json
import sys
from datetime import date, datetime, timedelta

import pytz

import database as db
from morning_brief import prev_trading_day
from sector_view import is_etf

IST = pytz.timezone('Asia/Kolkata')
KEY = 'watchdog_latest'
MIN_UNIVERSE = 400
GRADE_LAG_DAYS = 21          # calendar days past run_date with no grade = grader stuck
FUND_MAX_AGE = 45


def _rows(table, *filters, select='*', order=None, limit=None):
    q = db.supabase.table(table).select(select)
    for op, col, val in filters:
        q = getattr(q, op)(col, val)
    if order:
        q = q.order(order, desc=True)
    if limit:
        q = q.limit(limit)
    return q.execute().data or []


def _cfg(key):
    r = _rows('app_config', ('eq', 'key', key), select='value,updated_at', limit=1)
    return (r[0] if r else None) or {}


def _cfg_json(key):
    v = _cfg(key).get('value')
    return json.loads(v) if v else None


def _updated_on(cfg: dict, today: str) -> bool:
    return str(cfg.get('updated_at') or '')[:10] == today


def run_checks(today: str) -> list:
    """-> [{'name', 'status': PASS|WARN|FAIL, 'evidence', 'cause'}]."""
    out = []

    def add(name, ok, evidence, cause='', warn=False):
        out.append({'name': name, 'status': 'PASS' if ok else ('WARN' if warn else 'FAIL'),
                    'evidence': evidence, 'cause': '' if ok else cause})

    prev = prev_trading_day(today)
    official = _rows('portfolio_advice', ('eq', 'run_date', today), ('eq', 'is_official', True),
                     select='symbol,verdict,rotation_target_symbol,evaluated_at')
    nat_tok_today = _updated_on(_cfg('enc_token_natwar'), today)
    natwar = _rows('account_calls', ('eq', 'run_date', today),
                   select='symbol,verdict,rotation_target_symbol')
    held = {r['symbol'] for r in official} | {r['symbol'] for r in natwar}

    # Official run (V-16 shape): the day's record exists exactly once per name.
    add('official_run', bool(official), f"{len(official)} official rows {today}",
        "no live Kanaklata token before close, or the advisor crashed (Railway logs)")
    dups = len(official) - len({r['symbol'] for r in official})
    add('no_duplicate_advice', dups == 0, f"{dups} duplicate symbols", "two official batches landed for one day")

    # Universe scan (V-26).
    uni = _rows('universe_calls', ('eq', 'run_date', today), select='symbol')
    add('universe_scan', len(uni) >= MIN_UNIVERSE, f"{len(uni)} universe calls",
        "scan skipped or cut short — token pasted after the run, or Kite rate-limited",
        warn=bool(uni))

    # Second account (V-27) + the 11-13 shape: calls without targets.
    if nat_tok_today:
        add('natwar_calls', bool(natwar), f"{len(natwar)} Natwar calls",
            "token pasted but the preview never ran (not live, or ran before 09:45)")
        weak = [r for r in natwar if r['verdict'] in ('SELL', 'SELL_ON_BOUNCE', 'TRIM')]
        rot = [r for r in weak if r.get('rotation_target_symbol')]
        add('natwar_targets', not weak or bool(rot), f"{len(rot)}/{len(weak)} weak calls have a target",
            "preview ran before the primary scan landed (rerun fix 6c4c046)", warn=True)
    else:
        add('natwar_calls', True, "no Natwar token today — skipped")

    # ETF sleeve never rotated out (V-31).
    etf = [r['symbol'] for r in official + natwar if is_etf(r['symbol']) and r.get('rotation_target_symbol')]
    add('etf_not_rotated', not etf, f"{len(etf)} ETF rows with a target{': ' + ', '.join(etf) if etf else ''}",
        "ETF skip missing in a rotation path (brain 97d927d)")

    # Daily bars for every held name through the previous trading day (the
    # 11-12 Natwar price gap: reconcile marks a missing bar as a loss).
    if held:
        have = {r['symbol'] for r in _rows('candles', ('eq', 'interval', 'day'), ('eq', 'trade_date', prev),
                                           ('in_', 'symbol', sorted(held)), select='symbol')}
        missing = sorted(held - have)
        add('bars_fresh', not missing, f"{len(missing)}/{len(held)} held names missing the {prev} bar"
            + (f": {', '.join(missing[:6])}" + (' …' if len(missing) > 6 else '') if missing else ''),
            "bar sync skipped these (held outside Nifty 500? token died mid-sync?) — P&L for them is wrong")

    # Tape row (V-30).
    macro = _rows('macro_daily', ('eq', 'date', prev), select='date,data', limit=1)
    d = (macro[0]['data'] if macro else None) or {}
    nulls = [k for k in ('nifty', 'vix', 'usdinr', 'crude') if not d.get(k)]
    add('macro_row', bool(macro) and not nulls,
        f"macro_daily {prev}: " + ('missing' if not macro else ('nulls ' + ', '.join(nulls) if nulls else 'complete')),
        "morning bar sync did not run, or the futures front-month rollover failed")
    if macro:
        add('fii_row', d.get('fii_net_cr') is not None, f"fii_net_cr {d.get('fii_net_cr')}",
            "FIIDII 21:30 job not loaded or fetch 403 (FIIDII/fii_dii.log)", warn=True)

    # Real P&L landed (V-29).
    for acct, expect in (('primary', bool(official)), ('natwar', nat_tok_today)):
        if not expect:
            continue
        p = _rows('pnl_daily', ('eq', 'account', acct), ('eq', 'date', today), select='pnl,missing_prices', limit=1)
        add(f'pnl_{acct}', bool(p), f"pnl_daily {acct} {today}: " + (f"₹{p[0]['pnl']:,.0f}" if p else 'missing'),
            "post-close reconcile did not run — token expired before 15:35?")
        if p and p[0].get('missing_prices'):
            add(f'pnl_{acct}_prices', False, f"{p[0]['missing_prices']} names unpriced", "see bars_fresh", warn=True)

    # Grading keeps moving (V-23/26/27): nothing matured sits ungraded.
    cutoff = (date.fromisoformat(today) - timedelta(days=GRADE_LAG_DAYS)).isoformat()
    stuck = {t: len(_rows(t, ('lt', 'run_date', cutoff), ('is_', 'evaluated_at', 'null'),
                          *([('eq', 'is_official', True)] if t == 'portfolio_advice' else []), select='symbol'))
             for t in ('portfolio_advice', 'universe_calls', 'account_calls')}   # lite rows are never graded
    add('grading_moves', not any(stuck.values()),
        ', '.join(f"{t} {n}" for t, n in stuck.items()) + f" ungraded past {cutoff}",
        "grade_due / catch-up not running, or bars missing for the horizon")

    # Fundamentals age (quality scores stale at 45d).
    asof = (_cfg_json('fundamentals_latest') or {}).get('asof')
    age = (date.fromisoformat(today) - date.fromisoformat(asof)).days if asof else None
    add('fundamentals_age', age is not None and age <= FUND_MAX_AGE, f"fundamentals as of {asof} ({age}d)",
        "paste fresh Screener exports, scripts/ingest_fundamentals.py", warn=True)

    # Morning brief landed (V-32).
    brief = _cfg_json('morning_brief_latest') or {}
    add('brief_today', brief.get('run_date') == today, f"brief run_date {brief.get('run_date')}",
        "publish failed after the official run (Railway logs)", warn=True)
    return out


def run(today: str = None) -> dict:
    today = today or datetime.now(IST).date().isoformat()
    checks = run_checks(today)
    out = {'run_date': today, 'ran_at': datetime.now(IST).isoformat(), 'checks': checks,
           'fails': sum(c['status'] == 'FAIL' for c in checks),
           'warns': sum(c['status'] == 'WARN' for c in checks)}
    db.write_config(KEY, json.dumps(out, default=str))
    print(f"[watchdog] {today}: {out['fails']} FAIL, {out['warns']} WARN")
    for c in checks:
        if c['status'] != 'PASS':
            print(f"[watchdog]   {c['status']} {c['name']}: {c['evidence']} — {c['cause']}")
    return out


if __name__ == '__main__':
    run(sys.argv[1] if len(sys.argv) > 1 else None)
