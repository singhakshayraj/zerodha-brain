#!/usr/bin/env python3
"""[P-25] Recover the user's real executions from all advice history.

The live sync (scheduler → user_executions.run_user_executions) only looks back
a few days. This sweeps the whole `portfolio_advice` series once, so executions
that predate the feature are recovered too.

Idempotent — upserts on (symbol, side, quantity, detected_at), so re-running is
safe and will not duplicate.

    python3 scripts/backfill_user_executions.py [--dry-run] [--days N]
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

import database as db          # noqa: E402
import user_executions as ue   # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--dry-run', action='store_true',
                    help='detect and print, write nothing')
    ap.add_argument('--days', type=int, default=None,
                    help='limit lookback (default: all history)')
    args = ap.parse_args()

    snaps = db.get_advice_snapshots(lookback_days=args.days)
    print(f'snapshots: {len(snaps)} runs, '
          f'{snaps[0]["run_at"] if snaps else "-"} → '
          f'{snaps[-1]["run_at"] if snaps else "-"}')
    if not snaps:
        return 1

    execs = ue.detect_executions(snaps)
    print(f'detected: {len(execs)} executions\n')
    for e in execs:
        px = f"{e['price']:.2f}" if e.get('price') else '?'
        est = ' (est)' if e.get('price_is_estimated') else ''
        print(f"  {str(e['detected_at'])[:16]}  {e['side']:<4} "
              f"{e['symbol']:<12} x{e['quantity']:<6} @ {px}{est}   {e['notes']}")

    if args.dry_run:
        print('\n--dry-run: nothing written')
        return 0

    written = ue.sync_user_executions(lookback_days=args.days)
    print(f'\nwrote {written} rows to user_executions')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
