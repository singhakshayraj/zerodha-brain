"""Lab experiments [P-70]: one change at a time from a parent variant, replayed
on 2018-03..2024-12 only (2025 is the locked holdout). Each id is registered in
zerodha-trading docs/reference/LAB_EXPERIMENTS.md BEFORE it runs; a run
experiment is never edited, a new idea is a new id. Never registered live.

  python scripts/lab_backtest.py exp X01 [workers]
"""
import lab_strategies as ls
from lab_strategies import Variant


def _max_rank(fn, n):
    return lambda ctx, stop, entry: fn(ctx, stop, entry) if ctx.rank_adv <= n else None


ls.ARCHETYPES.setdefault('GAPFADE_TOP100', (_max_rank(ls._gapfade, 100),) + ls.ARCHETYPES['GAPFADE'][1:])

EXPERIMENTS = {
    'X01': [Variant('X01_GAPFADE_top100_v1', 'GAPFADE_TOP100', 0.5, 2.5)],
    'X02': [Variant('X02_GAPFADE_gap3.5_v1', 'GAPFADE', 0.5, 3.5)],
}
PARENT = {'X01': 'GAPFADE_s0.5_strict_v1', 'X02': 'GAPFADE_s0.5_strict_v1'}   # explore: gross PF 1.56, net 0.93
