"""Grade the advisor's BUY calls (rotation targets) — pre-registered
2026-11-11 in docs/reference/TRIAL_LEDGER.md (trials 254-255).

Every official portfolio_advice row with a rotation_target_symbol is a BUY
call on the target. Label: target close-to-close return over H trading days
from run_date minus Nifty 50's over the same bars; hit = alpha > 0.
Deciding stat: Fama-MacBeth across-date t on (hit - 0.5) vs the ledger
threshold. Read-only; uses stored daily bars, no Kite token. Separate from
the V-20 n=400 verdict pool.

    python3 scripts/grade_buy_calls.py
"""
import bisect
import importlib.util
import os
import statistics
import sys
from datetime import date, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
HORIZONS = (10, 30)
FOLLOW_WINDOW_DAYS = 7     # a real buy of the target within a week = followed


def _hero():
    spec = importlib.util.spec_from_file_location(
        'hero_picks', os.path.join(ROOT, 'scripts', 'hero_picks.py'))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def grade(calls: list, closes: dict, index: list, horizon: int) -> list:
    """calls: [{'run_date','target',...}]; closes: {sym: [(date, close)]}
    ascending; index likewise. Returns graded calls (matured only) with
    ret/nifty/alpha/hit added."""
    idx_dates = [d for d, _ in index]
    idx_px = dict(index)
    out = []
    for c in calls:
        bars = closes.get(c['target']) or []
        dates = [d for d, _ in bars]
        i0 = bisect.bisect_left(dates, c['run_date'])
        if i0 >= len(dates) or i0 + horizon >= len(dates):
            continue                      # no entry bar yet, or not matured
        d0, d1 = dates[i0], dates[i0 + horizon]
        if d0 not in idx_px or d1 not in idx_px:
            continue
        ret = bars[i0 + horizon][1] / bars[i0][1] - 1
        nifty = idx_px[d1] / idx_px[d0] - 1
        out.append({**c, 'entry_date': d0, 'exit_date': d1, 'ret': ret,
                    'nifty': nifty, 'alpha': ret - nifty,
                    'hit': 1 if ret > nifty else 0})
    return out


def summarize(graded: list) -> dict:
    import advisor_eval
    by_date = {}
    for g in graded:
        by_date.setdefault(g['run_date'], []).append(g['hit'] - 0.5)
    _, t, n_dates = advisor_eval.fama_macbeth(by_date)
    return {
        'n': len(graded),
        'dates': n_dates,
        'hit_rate': statistics.fmean(g['hit'] for g in graded) if graded else None,
        'mean_alpha': statistics.fmean(g['alpha'] for g in graded) if graded else None,
        'fm_t': t,
    }


def _fetch_calls():
    import database
    rows, off = [], 0
    while True:
        page = (database.supabase.table('portfolio_advice')
                .select('run_date,symbol,rotation_target_symbol')
                .eq('is_official', True)
                .not_.is_('rotation_target_symbol', 'null')
                .order('run_date').range(off, off + 999).execute().data) or []
        rows += page
        if len(page) < 1000:
            break
        off += 1000
    buys = (database.supabase.table('user_executions')
            .select('symbol,detected_at').eq('side', 'BUY').execute().data) or []
    bought = {}
    for b in buys:
        bought.setdefault(b['symbol'], []).append(str(b['detected_at'])[:10])
    calls = []
    for r in rows:
        rd, tgt = str(r['run_date']), r['rotation_target_symbol']
        until = (date.fromisoformat(rd) + timedelta(days=FOLLOW_WINDOW_DAYS)).isoformat()
        calls.append({'run_date': rd, 'sold': r['symbol'], 'target': tgt,
                      'followed': any(rd <= d <= until for d in bought.get(tgt, []))})
    return calls


def main():
    import advisor_eval
    threshold = advisor_eval.deflated_threshold(255)
    calls = _fetch_calls()
    hero = _hero()
    closes = hero.load_closes(sorted({c['target'] for c in calls}) + [hero.INDEX])
    index = closes.pop(hero.INDEX)
    print(f"BUY calls: {len(calls)} ({len({c['target'] for c in calls})} stocks), "
          f"{calls[0]['run_date']} -> {calls[-1]['run_date']}. Bar: |t| >= {threshold:.2f}\n")
    for h in HORIZONS:
        g = grade(calls, closes, index, h)
        s = summarize(g)
        if not s['n']:
            print(f"{h:>2}d: nothing matured yet")
            continue
        t = f"{s['fm_t']:+.2f}" if s['fm_t'] is not None else 'n/a'
        print(f"{h:>2}d: {s['n']} graded over {s['dates']} dates | beat Nifty "
              f"{s['hit_rate']*100:.1f}% | avg alpha {s['mean_alpha']*100:+.2f}% "
              f"| FM t {t} -> {'CLEARS' if s['fm_t'] and abs(s['fm_t']) >= threshold else 'does not clear'}")
        f = [x for x in g if x['followed']]
        if f:
            print(f"      you bought {len(f)}: " + ', '.join(
                f"{x['target']} {x['run_date']} {x['alpha']*100:+.1f}%" for x in f))
    pending = [c for c in calls if not grade([c], closes, index, HORIZONS[0])]
    print(f"\nNot yet matured at 10d: {len(pending)}")


if __name__ == '__main__':
    main()
