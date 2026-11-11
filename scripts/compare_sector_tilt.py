"""Trial 261: does the sector layer beat plain v1? (pre-registered 2026-11-11)

    python3 scripts/compare_sector_tilt.py

Unit: a graded call where sector_verdict != verdict (other rows are identical
by construction). Exposure kept: HOLD 1, TRIM 0.5, SELL/SELL_ON_BOUNCE 0.
Edge per call = (keep(sector) - keep(v1)) x alpha vs Nifty (pp): positive when
the layer kept more of a winner or less of a loser. Deciding statistic:
Fama-MacBeth t across run_dates of the per-date mean edge, against the
then-current deflated threshold. Reported separately for universe_calls and
account_calls; never pooled. Hit rates are descriptive only.
"""
import os
import sys
from statistics import mean, stdev

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

KEEP = {'HOLD': 1.0, 'TRIM': 0.5, 'SELL': 0.0, 'SELL_ON_BOUNCE': 0.0}
N_TRIALS = 261


def edges_by_date(rows: list) -> dict:
    """Pure: {run_date: [edge, ...]} over graded rows where the layer differs."""
    out = {}
    for r in rows:
        v, s, a = r.get('verdict'), r.get('sector_verdict'), r.get('outcome_vs_nifty_pct')
        if a is None or not s or s == v or v not in KEEP or s not in KEEP:
            continue
        out.setdefault(str(r['run_date']), []).append((KEEP[s] - KEEP[v]) * float(a))
    return out


def fm_t(by_date: dict):
    """(n_dates, mean of date means, t) or None with < 3 dates."""
    m = [mean(v) for v in by_date.values() if v]
    if len(m) < 3:
        return None
    sd = stdev(m)
    return len(m), mean(m), (mean(m) / (sd / len(m) ** 0.5) if sd else float('inf'))


def main():
    import database as db
    import universe_calls as uc
    from advisor_eval import deflated_threshold
    bar = deflated_threshold(N_TRIALS)
    for table in ('universe_calls', 'account_calls'):
        rows = uc._paged(lambda: db.supabase.table(table)
                         .select('run_date,verdict,sector_verdict,outcome_vs_nifty_pct,'
                                 'outcome_correct_alpha,sector_outcome_correct_alpha')
                         .not_.is_('evaluated_at', 'null').order('run_date'))
        by = edges_by_date(rows)
        n_calls = sum(len(v) for v in by.values())
        hits = [(r['outcome_correct_alpha'], r['sector_outcome_correct_alpha']) for r in rows
                if r.get('sector_verdict') and r['sector_verdict'] != r['verdict']
                and r['outcome_correct_alpha'] is not None]
        res = fm_t(by)
        print(f"\n{table}: {len(rows)} graded, {n_calls} where the layer differs, {len(by)} dates")
        if hits:
            print(f"  hit rate on those: v1 {mean(h[0] for h in hits) * 100:.1f}% · "
                  f"sector {mean(bool(h[1]) for h in hits) * 100:.1f}% (descriptive)")
        if res is None:
            print("  not enough dates for a read (need ≥ 3; first real read at ≥ 60)")
        else:
            n, m, t = res
            print(f"  edge per call {m:+.2f}pp · FM t {t:+.2f} over {n} dates · bar {bar:.2f} → "
                  f"{'CLEARS' if abs(t) >= bar else 'does not clear'}")


if __name__ == '__main__':
    main()
