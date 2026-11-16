"""Corporate-action patch list for the daily bar series [P-68 phase 0].

Kite's day bars are split/bonus adjusted but not demerger adjusted (ABFRL
2025-05-22 fell 56% on the ABLBL spin-out). data/corporate_actions.csv lists
(symbol, date, factor, kind, note): closes strictly BEFORE `date` are
multiplied by `factor` so a call scored across the action sees a continuous
series. Pure; the CSV is the only input.
"""
import csv
import os

ROOT = os.path.dirname(os.path.abspath(__file__))
PATH = os.path.join(ROOT, 'data', 'corporate_actions.csv')


def load(path: str = PATH) -> list:
    if not os.path.exists(path):
        return []
    with open(path) as fh:
        return [{**r, 'factor': float(r['factor'])} for r in csv.DictReader(fh)]


def adjust(bars: dict, actions: list = None) -> dict:
    """{symbol: {date: close}} with pre-action closes scaled. Untouched
    symbols are returned as the same objects."""
    actions = load() if actions is None else actions
    by = {}
    for a in actions:
        by.setdefault(a['symbol'], []).append(a)
    if not by:
        return bars
    out = dict(bars)
    for sym, acts in by.items():
        series = bars.get(sym)
        if not series:
            continue
        adj = {}
        for d, c in series.items():
            f = 1.0
            for a in acts:
                if d < a['date']:
                    f *= a['factor']
            adj[d] = c * f if f != 1.0 else c
        out[sym] = adj
    return out


def entry_factor(symbol: str, run_date: str, actions: list = None) -> float:
    """Scale for a price observed on `run_date`: the product of factors of
    actions dated after it (the entry is a pre-action price)."""
    actions = load() if actions is None else actions
    f = 1.0
    for a in actions:
        if a['symbol'] == symbol and run_date < a['date']:
            f *= a['factor']
    return f
