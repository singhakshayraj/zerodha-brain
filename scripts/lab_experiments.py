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


def _side_only(fn, side):
    def f(ctx, stop, entry):
        sig = fn(ctx, stop, entry)
        return sig if sig and sig['side'] == side else None
    return f


ls.ARCHETYPES.setdefault('GAPFADE_SHORT', (_side_only(ls._gapfade, 'SHORT'),) + ls.ARCHETYPES['GAPFADE'][1:])
ls.ARCHETYPES.setdefault('GAPFADE_SHORT_TOP100', (_max_rank(_side_only(ls._gapfade, 'SHORT'), 100),) + ls.ARCHETYPES['GAPFADE'][1:])
ls.ARCHETYPES.setdefault('GAPFADE_TOP100', (_max_rank(ls._gapfade, 100),) + ls.ARCHETYPES['GAPFADE'][1:])

EXPERIMENTS = {
    'X01': [Variant('X01_GAPFADE_top100_v1', 'GAPFADE_TOP100', 0.5, 2.5)],
    'X02': [Variant('X02_GAPFADE_gap3.5_v1', 'GAPFADE', 0.5, 3.5)],
    'X03': [Variant('X03_GAPFADE_short_v1', 'GAPFADE_SHORT', 0.5, 2.5)],
    'X04': [Variant('X04_GAPFADE_short_top100_v1', 'GAPFADE_SHORT_TOP100', 0.5, 2.5)],
    'X05': [Variant('X05_GAPFADE_short_top100_gap1.5_v1', 'GAPFADE_SHORT_TOP100', 0.5, 1.5)],
}
PARENT = {'X01': 'GAPFADE_s0.5_strict_v1', 'X02': 'GAPFADE_s0.5_strict_v1', 'X03': 'GAPFADE_s0.5_strict_v1',
          'X04': 'X03_GAPFADE_short_v1', 'X05': 'X04_GAPFADE_short_top100_v1'}   # explore: gross PF 1.56, net 0.93
