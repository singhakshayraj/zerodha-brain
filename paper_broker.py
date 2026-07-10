"""Paper-trading execution layer.

Drop-in replacement for OrderManager: identical public interface, but instead
of placing real Kite orders it simulates fills at the LIVE last-traded price
(via kite.get_ltp — a read-only quote call) plus configurable slippage.

Everything upstream — market data, indicators, signal engine, risk manager,
regime detector, database writes — runs unchanged, so a paper session
exercises the full decision pipeline on real market data with zero order risk.

Selected in brain.py when config.PAPER_TRADING is true.
"""

import uuid

import config
import database as db
from kite_client import KiteClient


def _paper_order_id() -> str:
    return f"PAPER-{uuid.uuid4().hex[:12]}"


def _zerodha_intraday_charges(side: str, price: float, quantity: int) -> float:
    """Real Zerodha equity intraday (MIS) charges for one order leg, in ₹.

    Without these, paper P&L overstates the edge — for intraday, transaction
    costs are often the difference between a profitable and losing strategy.

    Schedule (NSE equity intraday, as of 2025):
      brokerage: min(₹20, 0.03% of turnover) per executed order
      STT:       0.025% on the SELL side only
      exchange:  0.00297% of turnover (NSE)
      SEBI:      0.0001% of turnover
      GST:       18% on (brokerage + exchange + SEBI)
      stamp:     0.003% on the BUY side only
    """
    turnover = price * quantity
    brokerage = min(20.0, turnover * 0.0003)
    stt = turnover * 0.00025 if side == 'SELL' else 0.0
    exchange = turnover * 0.0000297
    sebi = turnover * 0.000001
    gst = 0.18 * (brokerage + exchange + sebi)
    stamp = turnover * 0.00003 if side == 'BUY' else 0.0
    return round(brokerage + stt + exchange + sebi + gst + stamp, 2)


class PaperBroker:

    def __init__(self):
        self.session_id = None

    # ── internals ────────────────────────────────────────────────────────────

    def _clean_symbol(self, symbol: str) -> str:
        return symbol.replace('NSE:', '').replace('BSE:', '')

    def _live_price(self, kite: KiteClient, symbol: str, exchange: str):
        """Fetch real LTP for the instrument. Returns None if unavailable —
        a paper fill without a real price would poison the dataset."""
        instrument = f"{exchange}:{self._clean_symbol(symbol)}"
        try:
            data = kite.get_ltp([instrument]) or {}
            quote = data.get(instrument) or {}
            price = quote.get('last_price') or 0
            return price if price > 0 else None
        except Exception as e:
            print(f"[PAPER] LTP fetch failed for {instrument}: {e}")
            return None

    def _fill(self, kite: KiteClient, symbol: str, exchange: str,
              quantity: int, side: str, hint_price: float = None):
        """Simulate a MARKET fill at live LTP adjusted for slippage.
        side: 'BUY' pays up, 'SELL' receives less — always adverse.

        hint_price: the live price the brain already fetched when it made the
        decision (holdings cache / candle close). Used when the LTP quote
        endpoint is unavailable — /quote does not work with retail enctoken
        auth (see TRADING_MODE_FORCE in config.py), so without this fallback
        every paper fill fails."""
        ltp = self._live_price(kite, symbol, exchange)
        if ltp is None and hint_price and hint_price > 0:
            ltp = hint_price
        if ltp is None:
            print(f"[PAPER] {side} order failed for {symbol}: no live price")
            if self.session_id:
                try:
                    db.log_brain_activity(
                        self.session_id,
                        'ORDER_FAILED',
                        symbol=symbol,
                        message=f'[PAPER] {side} failed: no live price',
                        data={'paper': True},
                    )
                except Exception:
                    pass
            return None

        slip = config.PAPER_SLIPPAGE_PCT / 100.0
        price = ltp * (1 + slip) if side == 'BUY' else ltp * (1 - slip)

        # Fold real transaction charges into the fill price adversely
        # (charges/share on top of slippage), so they flow through
        # entry_price/exit_price into P&L without any schema change.
        charges = _zerodha_intraday_charges(side, price, quantity)
        per_share = charges / quantity if quantity else 0.0
        price = round(price + per_share if side == 'BUY' else price - per_share, 2)
        order_id = _paper_order_id()

        # Slippage decomposition (Tier-2): the fill folds slippage + charges
        # into one adverse price. Surface the reference (intended) price and
        # the total adverse deviation in bps so analysis can separate execution
        # cost from signal edge (and later link latency → slippage).
        ref = round(ltp, 2)
        adverse = (price - ltp) if side == 'BUY' else (ltp - price)
        slippage_bps = round((adverse / ltp) * 10000, 2) if ltp else 0.0

        print(
            f"[PAPER] {side} filled: {symbol} x{quantity} @ ₹{price} "
            f"(ltp {ltp}, slippage {config.PAPER_SLIPPAGE_PCT}%, "
            f"charges ₹{charges}, {slippage_bps}bps adverse) [{order_id}]"
        )
        return {
            'order_id': order_id,
            'status': 'COMPLETE',
            'price': price,
            'quantity': quantity,
            'value': price * quantity,
            'reference_price': ref,
            'slippage_bps': slippage_bps,
        }

    # ── OrderManager-compatible interface ────────────────────────────────────

    def place_buy_order(self, kite: KiteClient, symbol: str, exchange: str,
                        quantity: int, hint_price: float = None):
        print(f"[PAPER] BUY order: {symbol} x{quantity}")
        return self._fill(kite, symbol, exchange, quantity, 'BUY', hint_price)

    def place_sell_order(self, kite: KiteClient, symbol: str, exchange: str,
                         quantity: int, hint_price: float = None):
        # No CNC safety lock needed: nothing real can be sold. The brain only
        # calls this to close paper longs it opened itself.
        print(f"[PAPER] SELL order: {symbol} x{quantity}")
        return self._fill(kite, symbol, exchange, quantity, 'SELL', hint_price)

    def place_short_order(self, kite: KiteClient, symbol: str, exchange: str,
                          quantity: int, hint_price: float = None):
        print(f"[PAPER] SHORT order: {symbol} x{quantity}")
        return self._fill(kite, symbol, exchange, quantity, 'SELL', hint_price)

    def cover_short_order(self, kite: KiteClient, symbol: str, exchange: str,
                          quantity: int, hint_price: float = None):
        print(f"[PAPER] COVER order: {symbol} x{quantity}")
        return self._fill(kite, symbol, exchange, quantity, 'BUY', hint_price)

    def square_off_all(self, kite: KiteClient, open_trades: list) -> None:
        # SHORTs must be bought back (cover), not sold again — selling an
        # open short here would fabricate a phantom SELL fill instead of
        # closing the position, corrupting the trade's exit side and P&L.
        print(f"[PAPER] Squaring off {len(open_trades)} open positions")
        for trade in open_trades:
            closer = (self.cover_short_order if trade.get('position_type') == 'SHORT'
                      else self.place_sell_order)
            closer(
                kite,
                trade['symbol'],
                trade['exchange'],
                trade['quantity'],
            )
