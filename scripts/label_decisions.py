#!/usr/bin/env python3
"""Track C counterfactual decision labeling — manual/standalone entry
point (2026-07-15). NOT wired into the live scheduler; run on-demand:

    python3 scripts/label_decisions.py 2026-07-15
    python3 scripts/label_decisions.py 2026-07-15 2026-07-16   # a range

No enc_token needed — reads only brain_decisions + candles, both already
in Supabase. See docs/ML_TRACK_C_NOTES.md (zerodha-trading repo) and
decision_outcomes.py for the design/limitations.
"""
import os
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import decision_outcomes


def _daterange(start: str, end: str):
    d0 = date.fromisoformat(start)
    d1 = date.fromisoformat(end)
    d = d0
    while d <= d1:
        yield d.isoformat()
        d += timedelta(days=1)


if __name__ == '__main__':
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    start = sys.argv[1]
    end = sys.argv[2] if len(sys.argv) > 2 else start
    total = 0
    for run_date in _daterange(start, end):
        total += decision_outcomes.label_decisions_for_date(run_date)
    print(f"[label_decisions] total labeled: {total}")
