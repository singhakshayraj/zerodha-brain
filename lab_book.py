"""Strategy-lab paper book [P-70]: one virtual ₹50k MIS account per strategy.

Fills, Zerodha intraday charges, slippage, R accounting and the MIS margin
cap (gross open notional <= capital x leverage — the invariant the old engine
never had, which let it reach 7.85x). Paper only: nothing here talks to Kite.
"""
from dataclasses import dataclass
from typing import Optional

from paper_broker import _zerodha_intraday_charges

STOP_CAP_R = 0.25   # stop fill never worse than stop - 0.25R (poll-latency band)


@dataclass
class Position:
    strategy_id: str
    symbol: str
    side: str                 # 'LONG' | 'SHORT'
    qty: int
    entry_price: float        # fill, after slippage
    entry_ts: str
    stop: float
    target: Optional[float]
    slip_bps: float
    risk_inr: float
    entry_charges: float
    entry_slip_inr: float
    entry_ref: float = 0.0    # pre-slippage reference (LTP) — gross P&L + margin use it
    db_id: Optional[int] = None


class Book:
    def __init__(self, strategy_id: str, capital: float = 50000.0,
                 leverage: float = 5.0, notional: float = 25000.0):
        self.strategy_id = strategy_id
        self.cap = capital * leverage
        self.notional = notional
        self.open: list = []
        self.entries_today: dict = {}

    def try_open(self, symbol, side, ltp, stop, target, slip_bps, ts):
        qty = int(self.notional // ltp) if ltp > 0 else 0
        if qty < 1:
            return 'qty'
        if self.entries_today.get(symbol, 0) >= 3:
            return 'symbol_limit'
        used = sum(p.qty * p.entry_ref for p in self.open)
        if used + qty * ltp > self.cap + 1e-6:
            return 'margin'
        sign = 1 if side == 'LONG' else -1
        fill = ltp * (1 + sign * slip_bps / 1e4)
        pos = Position(self.strategy_id, symbol, side, qty, fill, ts, stop, target,
                       slip_bps, abs(fill - stop) * qty,
                       _zerodha_intraday_charges('BUY' if side == 'LONG' else 'SELL', fill, qty),
                       abs(fill - ltp) * qty, entry_ref=ltp)
        self.open.append(pos)
        self.entries_today[symbol] = self.entries_today.get(symbol, 0) + 1
        return pos

    def check_exit(self, pos: Position, ltp: float):
        if pos.side == 'LONG':
            if ltp <= pos.stop:
                return 'STOP'
            if pos.target is not None and ltp >= pos.target:
                return 'TARGET'
        else:
            if ltp >= pos.stop:
                return 'STOP'
            if pos.target is not None and ltp <= pos.target:
                return 'TARGET'
        return None

    def close(self, pos: Position, ltp: float, ts: str, reason: str) -> dict:
        sign = 1 if pos.side == 'LONG' else -1
        if reason == 'STOP':
            r_ps = abs(pos.entry_price - pos.stop)
            worst = pos.stop - sign * STOP_CAP_R * r_ps
            ref = fill = (max(min(ltp, pos.stop), worst) if sign == 1
                          else min(max(ltp, pos.stop), worst))
        else:
            ref = pos.target if reason == 'TARGET' else ltp
            fill = ref * (1 - sign * pos.slip_bps / 1e4)
        exit_charges = _zerodha_intraday_charges('SELL' if sign == 1 else 'BUY', fill, pos.qty)
        gross = sign * (ref - pos.entry_ref) * pos.qty
        charges = round(pos.entry_charges + exit_charges, 2)
        slip = round(pos.entry_slip_inr + abs(fill - ref) * pos.qty, 2)
        net = round(gross - charges - slip, 2)
        risk = pos.risk_inr or 1e-9
        if pos in self.open:
            self.open.remove(pos)
        return {'strategy_id': pos.strategy_id, 'symbol': pos.symbol, 'side': pos.side,
                'qty': pos.qty, 'entry_ts': pos.entry_ts, 'exit_ts': ts,
                'entry_price': pos.entry_price, 'exit_price': fill, 'stop': pos.stop,
                'target': pos.target, 'exit_reason': reason, 'gross_inr': round(gross, 2),
                'charges_inr': charges, 'slippage_inr': slip, 'net_inr': net,
                'gross_r': gross / risk, 'cost_r': (charges + slip) / risk, 'net_r': net / risk}
