"""Grade the SHADOW thesis engine (P-47) — pre-registered trials 256–257.

For every official advice row carrying indicators.thesis, the holding's
close-to-close alpha vs Nifty 50 over 126 and 252 trading days from
run_date (stored daily bars; no token). hit = (action in HOLD/ADD) ==
(alpha > 0). Deciding statistic: Fama–MacBeth across-date t on (hit − 0.5)
vs the trial-ledger threshold. Writes the aggregate to
app_config['thesis_track_record']. Run monthly:

    python3 scripts/grade_thesis.py
"""
import bisect
import importlib.util
import json
import os
import statistics
import sys
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
HORIZONS = (126, 252)
LONG = ('HOLD', 'ADD')


def _hero():
    spec = importlib.util.spec_from_file_location(
        'hero_picks', os.path.join(ROOT, 'scripts', 'hero_picks.py'))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def grade(theses: list, closes: dict, index: list, horizon: int) -> list:
    idx_px = dict(index)
    out = []
    for t in theses:
        bars = closes.get(t['symbol']) or []
        dates = [d for d, _ in bars]
        i0 = bisect.bisect_left(dates, t['run_date'])
        if i0 >= len(dates) or i0 + horizon >= len(dates):
            continue
        d0, d1 = dates[i0], dates[i0 + horizon]
        if d0 not in idx_px or d1 not in idx_px:
            continue
        alpha = (bars[i0 + horizon][1] / bars[i0][1] - 1) - (idx_px[d1] / idx_px[d0] - 1)
        out.append({**t, 'alpha': alpha,
                    'hit': 1 if (t['action'] in LONG) == (alpha > 0) else 0})
    return out


def summarize(graded: list, threshold: float) -> dict:
    import advisor_eval
    by_date = {}
    for g in graded:
        by_date.setdefault(g['run_date'], []).append(g['hit'] - 0.5)
    _, t, n_dates = advisor_eval.fama_macbeth(by_date)
    longs = [g['alpha'] for g in graded if g['action'] in LONG]
    exits = [g['alpha'] for g in graded if g['action'] not in LONG]
    return {'n': len(graded), 'dates': n_dates,
            'hit_rate': round(statistics.fmean(g['hit'] for g in graded), 4) if graded else None,
            'fm_t': round(t, 2) if t is not None else None,
            'clears': bool(t is not None and abs(t) >= threshold),
            'long_basket_alpha': round(statistics.fmean(longs), 4) if longs else None,
            'exit_basket_alpha': round(statistics.fmean(exits), 4) if exits else None}


def _fetch():
    import database
    rows, off = [], 0
    while True:
        page = (database.supabase.table('portfolio_advice')
                .select('run_date,symbol,indicators').eq('is_official', True)
                .order('run_date').range(off, off + 999).execute().data) or []
        rows += page
        if len(page) < 1000:
            break
        off += 1000
    return [{'run_date': str(r['run_date']), 'symbol': r['symbol'],
             'action': r['indicators']['thesis']['action']}
            for r in rows if isinstance(r.get('indicators'), dict)
            and (r['indicators'].get('thesis') or {}).get('action')]


def main():
    import advisor_eval
    import database as db
    threshold = advisor_eval.deflated_threshold(257)
    theses = _fetch()
    if not theses:
        print('no thesis rows yet'); return
    hero = _hero()
    closes = hero.load_closes(sorted({t['symbol'] for t in theses}) + [hero.INDEX])
    index = closes.pop(hero.INDEX)
    record = {'graded_at': datetime.now().date().isoformat(), 'theses': len(theses),
              'first_run_date': theses[0]['run_date'], 'threshold': round(threshold, 2)}
    print(f"theses: {len(theses)} from {theses[0]['run_date']}; bar |t| >= {threshold:.2f}")
    for h in HORIZONS:
        s = summarize(grade(theses, closes, index, h), threshold)
        record[f'h{h}'] = s
        print(f"{h:>3}d: " + (f"{s['n']} graded over {s['dates']} dates | hit {s['hit_rate']} "
                              f"| FM t {s['fm_t']} -> {'CLEARS' if s['clears'] else 'does not clear'} "
                              f"| long basket alpha {s['long_basket_alpha']} | exit basket {s['exit_basket_alpha']}"
                              if s['n'] else 'nothing matured yet'))
    db.write_config('thesis_track_record', json.dumps(record))


if __name__ == '__main__':
    main()
