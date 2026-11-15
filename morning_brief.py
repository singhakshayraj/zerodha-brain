"""[P-55] Morning brief: at most six plain lines after the day's runs —
moves, what changed since the last run, household skew, the tape, and
anything stale. Display only; written to app_config 'morning_brief_latest'
by the official run and again after the second-account preview so both
books are in it. compose() is pure; publish() loads and writes."""
import json
from datetime import date, timedelta

import config
import database as db

KEY = 'morning_brief_latest'
WEAK = ('SELL', 'SELL_ON_BOUNCE', 'TRIM')
MAX_PAIRS = 4        # one phone line per account; the move cards have the rest
SKEW_PP = 5          # overweight line when you vs index differs by this much


def prev_trading_day(today: str) -> str:
    d = date.fromisoformat(today) - timedelta(days=1)
    while d.weekday() > 4 or d.isoformat() in config.NSE_HOLIDAYS:
        d -= timedelta(days=1)
    return d.isoformat()


def _moves(label: str, rows: list) -> str:
    weak = [r for r in rows if r.get('verdict') in WEAK]
    pairs = [f"{r['symbol']}→{r['rotation_target_symbol']}" for r in weak if r.get('rotation_target_symbol')]
    if not weak:
        return f"{label}: no moves"
    sells = sum(r.get('verdict') != 'TRIM' for r in weak)
    head = f"{label}: {sells} sell, {len(weak) - sells} trim"
    if len(pairs) > MAX_PAIRS:
        pairs = pairs[:MAX_PAIRS] + [f"+{len(pairs) - MAX_PAIRS} more"]
    return head + (f" → {', '.join(pairs)}" if pairs else " (no targets)")


def _skew(label: str, view: dict) -> list:
    out = []
    for g in (view or {}).get('groups') or []:
        if g.get('index_weight_pct') is None or g['group'] in ('Outside Nifty 500',):
            continue
        if g['my_weight_pct'] - g['index_weight_pct'] >= SKEW_PP:
            out.append(f"{g['group']} {g['my_weight_pct']:.0f}% vs {g['index_weight_pct']:.0f}% ({label})")
    return out


def compose(today: str, primary: list, prior: list, prior_date, natwar: dict,
            views: dict, macro: dict, fund_asof, watchdog: dict = None) -> list:
    """-> [{'kind', 'text', 'warn'}]. primary/prior: advisor rows; natwar:
    the preview payload or None; views: {label: sector_view}; macro: latest
    macro_daily row or None; fund_asof: fundamentals store date or None."""
    lines = []
    nat_rows = (natwar or {}).get('rows') or []
    lines.append({'kind': 'moves', 'warn': False,
                  'text': _moves('Kanaklata', primary) + ' · ' + (_moves('Natwar', nat_rows) if natwar else 'Natwar: no run')})
    if prior:
        was = {r['symbol']: r.get('verdict') for r in prior}
        flips = [f"{r['symbol']} {was[r['symbol']]}→{r['verdict']}" for r in primary
                 if r['symbol'] in was and was[r['symbol']] != r.get('verdict')]
        new = [r['symbol'] for r in primary if r['symbol'] not in was]
        bits = flips + [f"+{s} new" for s in new]
        lines.append({'kind': 'changes', 'warn': False,
                      'text': f"Since {prior_date}: " + (', '.join(bits) if bits else 'no verdict changes')})
    skew = [s for label, v in views.items() for s in _skew(label, v)][:3]
    if skew:
        lines.append({'kind': 'household', 'warn': False, 'text': 'Overweight: ' + '; '.join(skew)})
    stale = []
    prev = prev_trading_day(today)
    if macro:
        d = macro['data']
        n, vix, br, fii = d.get('nifty') or {}, d.get('vix') or {}, d.get('breadth') or {}, d.get('fii_net_cr')
        tape = [f"Nifty {n.get('close', 0):,.0f} ({n.get('d1', 0):+.1f}%, 5d {n.get('d5', 0):+.1f}%)"]
        if vix:
            tape.append(f"VIX {vix.get('close')}")
        if br:
            tape.append(f"breadth {br.get('pct_trend_up')}% up")
        tape.append(f"FII ₹{fii:,.0f} cr" if fii is not None else "FII —")
        lines.append({'kind': 'tape', 'warn': False, 'text': f"Tape {macro['date']}: " + ' · '.join(tape)})
        if macro['date'] < prev:
            stale.append(f"tape data stops at {macro['date']}")
        if fii is None:
            stale.append("no FII/DII row (is the 21:30 fetch job loaded?)")
    else:
        stale.append("no tape data")
    if natwar and natwar.get('run_date') != today:
        stale.append(f"Natwar last ran {natwar.get('run_date')}")
    if fund_asof and (date.fromisoformat(today) - date.fromisoformat(fund_asof)).days > 30:
        stale.append(f"fundamentals {fund_asof}")
    if stale:
        lines.append({'kind': 'stale', 'warn': True, 'text': 'Stale: ' + '; '.join(stale)})
    bad = [c for c in (watchdog or {}).get('checks') or [] if c['status'] != 'PASS']
    if bad and (watchdog or {}).get('run_date') == prev:
        lines.append({'kind': 'watch', 'warn': any(c['status'] == 'FAIL' for c in bad),
                      'text': f"Last session ({prev}): " + '; '.join(f"{c['status']} {c['name']} — {c['evidence']}" for c in bad[:3])
                      + (f" (+{len(bad) - 3} more)" if len(bad) > 3 else '')})
    return lines[:6]


def _cfg_json(key: str):
    raw = db.get_config(key)
    return json.loads(raw) if raw else None


def publish(today: str) -> dict:
    sel = 'symbol,verdict,rotation_target_symbol'
    primary = (db.supabase.table('portfolio_advice').select(sel)
               .eq('run_date', today).eq('is_official', True).execute().data) or []
    prior_rows = (db.supabase.table('portfolio_advice').select('run_date')
                  .lt('run_date', today).eq('is_official', True)
                  .order('run_date', desc=True).limit(1).execute().data) or []
    prior_date = prior_rows[0]['run_date'] if prior_rows else None
    prior = (db.supabase.table('portfolio_advice').select(sel)
             .eq('run_date', prior_date).eq('is_official', True).execute().data) if prior_date else []
    natwar = _cfg_json('advisor_natwar_latest')
    views = {'Kanaklata': _cfg_json('sector_view_latest'),
             'Natwar': (natwar or {}).get('sector_view')}
    macro = (db.supabase.table('macro_daily').select('date,data')
             .order('date', desc=True).limit(1).execute().data or [None])[0]
    fund_asof = (_cfg_json('fundamentals_latest') or {}).get('asof')
    out = {'run_date': today, 'lines': compose(today, primary, prior, prior_date, natwar, views,
                                               macro, fund_asof, _cfg_json('watchdog_latest'))}
    db.write_config(KEY, json.dumps(out, default=str))
    print(f"[brief] {len(out['lines'])} lines")
    return out
