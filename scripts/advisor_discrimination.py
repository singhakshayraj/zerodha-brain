#!/usr/bin/env python3
"""Does the advisor's confidence number separate right calls from wrong ones?

WHY THIS EXISTS: the headline "AUC 0.4556" sat in four documents and in no
code. It could not be regenerated -- not from `confidence`, `trend_score` or
`calibrated_confidence`, and not under any tie convention. A number nobody can
recompute is not evidence, so this makes it a command instead of a memory.

It also reports the metric under TWO labels, because the stored one is
market-confounded:

  outcome_correct  HOLD right when the stock ROSE, exits right when it FELL.
                   In a rising tape every HOLD scores right regardless of
                   quality -- it measures beta as much as skill.
  alpha            the same test against outcome_vs_nifty_pct, which the
                   grader already computes and stores and then never uses.

Ties matter here: ~4% of confidence pairs are exact ties, so the Mann-Whitney
0.5 credit is used (scoring ties as 0 drags the AUC down by ~0.02).

Usage:  python3 scripts/advisor_discrimination.py
"""
import os
import statistics
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import database as db

EXIT_VERDICTS = {'SELL', 'SELL_ON_BOUNCE', 'TRIM'}


def auc(pairs):
    """Mann-Whitney AUC. Ties score 0.5 -- the standard convention."""
    pos = [s for s, y in pairs if y]
    neg = [s for s, y in pairs if not y]
    if not pos or not neg:
        return None
    wins = sum(1.0 if p > n else 0.5 if p == n else 0.0
               for p in pos for n in neg)
    return wins / (len(pos) * len(neg))


def label_absolute(row):
    return bool(row['outcome_correct'])


def label_alpha(row):
    a = row.get('outcome_vs_nifty_pct')
    if a is None:
        return None
    if row.get('verdict') == 'HOLD':
        return a > 0
    if row.get('verdict') in EXIT_VERDICTS:
        return a < 0
    return None


def main() -> int:
    rows = (db.supabase.table('portfolio_advice')
            .select('verdict,confidence,outcome_correct,outcome_vs_nifty_pct')
            .not_.is_('outcome_correct', 'null')
            .limit(1000).execute().data) or []
    rows = [r for r in rows if r.get('confidence') is not None]
    if not rows:
        print('no graded advice with a confidence value yet')
        return 1

    print(f"graded calls with confidence: {len(rows)}\n")
    for name, fn in (('outcome_correct (absolute — stored)', label_absolute),
                     ('alpha vs Nifty (market-neutral)', label_alpha)):
        pairs = [(r['confidence'], fn(r)) for r in rows if fn(r) is not None]
        if not pairs:
            continue
        a = auc(pairs)
        right = [s for s, y in pairs if y]
        wrong = [s for s, y in pairs if not y]
        print(f"{name}")
        print(f"  n={len(pairs)}  hit_rate={len(right)/len(pairs):.3f}  "
              f"AUC={a:.4f}")
        if right and wrong:
            print(f"  mean confidence  right {statistics.fmean(right):.1f}  "
                  f"wrong {statistics.fmean(wrong):.1f}")
        print()

    print("0.5 = coin flip. Re-open the confidence question only on an AUC")
    print("materially above 0.5 under BOTH labels.")
    return 0


if __name__ == '__main__':
    sys.exit(main())
