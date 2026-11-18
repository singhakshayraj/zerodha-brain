"""S5 trend-filtered momentum — a ₹50k PAPER book [P-71, trial 265].

Rules are frozen in zerodha-trading docs/reference/s1_momentum/PREREG_S5.md:
S1-T from the S1 kit's backtest.py plus Addendum B (live adaptations). Monthly:
top 10 of the 200 most liquid eligible Nifty-500 names by vol-scaled 6/12-month
momentum, a rank-20 buffer, all cash when Nifty 50 is below its 200-session SMA.
Paper only — this module never places a broker order.
"""
import math
from statistics import median

import numpy as np

import corporate_actions

# Constants copied verbatim from the S1 kit (backtest.py) — a change is a new trial.
N_HOLD, BUFFER, TOP_LIQ = 10, 20, 200
MIN_HIST, MIN_PRICE = 260, 20.0
SLIP = 0.0010                     # 10 bps per leg
STT, STAMP = 0.001, 0.00015
TXN, SEBI, GST = 0.0000307, 0.000001, 0.18
DP = 15.34                        # per scrip per sell day
LIQ_WIN, LIQ_MIN = 126, 100
VOL_WIN, VOL_MIN = 252, 200
R6, R12, SMA = 126, 252, 200


def _day(b) -> str:
    return str(b.get('timestamp') or '')[:10]


def _upto(bars: list, asof: str) -> list:
    return [b for b in bars or [] if _day(b) < asof and b.get('close') is not None]


def _z(x: np.ndarray) -> np.ndarray:
    sd = x.std(ddof=1) if len(x) > 1 else 0.0
    return (x - x.mean()) / sd if sd else np.zeros_like(x)


def rank(bars: dict, nifty: list, asof: str) -> dict:
    """Signal from bars dated strictly before `asof` (the rebalance session).
    Returns {'ranked': top-TOP_LIQ liquid eligible names, best score first,
    'risk_off': Nifty 50 < its 200-SMA, 'eligible': count before the cut}.
    Raises ValueError when Nifty history is too short to decide risk-off."""
    nc = [float(b['close']) for b in _upto(nifty, asof)]
    if len(nc) < SMA:
        raise ValueError(f'nifty history {len(nc)} < {SMA} bars')
    risk_off = nc[-1] < sum(nc[-SMA:]) / SMA

    raw = {s: {_day(b): float(b['close']) for b in _upto(bs, asof)} for s, bs in bars.items()}
    adj = corporate_actions.adjust(raw)
    rows = []
    for s, series in adj.items():
        days = sorted(series)
        if len(days) < MIN_HIST or raw[s][days[-1]] < MIN_PRICE:
            continue
        c = np.array([series[d] for d in days])
        lr = np.diff(np.log(c))[-VOL_WIN:]
        vol = {_day(b): float(b.get('volume') or 0) for b in bars[s]}
        val = [raw[s][d] * vol.get(d, 0.0) for d in days[-LIQ_WIN:]]
        if len(lr) < VOL_MIN or len(val) < LIQ_MIN:
            continue
        sigma = lr.std(ddof=1) * math.sqrt(252)
        if not sigma:
            continue
        rows.append((s, median(val), (c[-1] / c[-1 - R6] - 1) / sigma,
                     (c[-1] / c[-1 - R12] - 1) / sigma))
    eligible = len(rows)
    rows = sorted(rows, key=lambda r: (-r[1], r[0]))[:TOP_LIQ]
    if not rows:
        return {'ranked': [], 'risk_off': risk_off, 'eligible': eligible}
    score = 0.5 * _z(np.array([r[2] for r in rows])) + 0.5 * _z(np.array([r[3] for r in rows]))
    order = sorted(range(len(rows)), key=lambda i: (-score[i], rows[i][0]))
    return {'ranked': [rows[i][0] for i in order], 'risk_off': risk_off, 'eligible': eligible}
