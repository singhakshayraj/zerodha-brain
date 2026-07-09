import time

import config
import database as db
from kite_client import KiteClient


class OrderManager:

    def __init__(self):
        self.session_id = None

    def place_buy_order(
        self,
        kite: KiteClient,
        symbol: str,
        exchange: str,
        quantity: int,
    ):
        print(f"Placing BUY order: {symbol} x{quantity}")

        order_id = kite.place_order(
            symbol=symbol,
            exchange=exchange,
            transaction_type='BUY',
            quantity=quantity,
            order_type='MARKET',
            product='MIS',
            variety='regular',
        )

        if not order_id:
            print(f"BUY order failed for {symbol}")
            return None

        time.sleep(config.ORDER_CONFIRMATION_WAIT_SECONDS)
        order = kite.get_order_status(order_id)

        if order and order.get('status') == 'COMPLETE':
            avg_price = order.get('average_price', 0) or 0
            filled_qty = order.get('filled_quantity', quantity) or quantity
            print(f"BUY confirmed: {symbol} x{filled_qty} @ ₹{avg_price}")
            return {
                'order_id': order_id,
                'status': 'COMPLETE',
                'price': avg_price,
                'quantity': filled_qty,
                'value': avg_price * filled_qty,
            }
        else:
            status = order.get('status', 'UNKNOWN') if order else 'UNKNOWN'
            print(f"BUY order status: {status} for {symbol}")
            return None

    def _check_safety_sell(self, kite: KiteClient, symbol: str) -> bool:
        """Return True if SELL is safe (MIS long exists). False blocks."""
        clean_symbol = symbol.replace('NSE:', '').replace('BSE:', '')
        try:
            positions = kite.get_positions() or {}
            net = positions.get('net', []) if isinstance(positions, dict) else []

            for p in net:
                if (
                    p.get('tradingsymbol') == clean_symbol
                    and p.get('product') == 'MIS'
                    and (p.get('quantity') or 0) > 0
                ):
                    return True

            print(f"[SAFETY] BLOCKED SELL on {symbol}")
            print(f"[SAFETY] Reason: No intraday MIS position")
            print(f"[SAFETY] CNC holdings will NOT be touched")

            if self.session_id:
                try:
                    db.log_brain_activity(
                        self.session_id,
                        'ORDER_FAILED',
                        symbol=symbol,
                        message='SELL blocked: would reduce CNC holding',
                        data={'safety_lock': True},
                    )
                except Exception:
                    pass

            return False

        except Exception as e:
            print(f"[SAFETY] Position check failed: {e}")
            print(f"[SAFETY] BLOCKING order as precaution")
            return False

    def place_sell_order(
        self,
        kite: KiteClient,
        symbol: str,
        exchange: str,
        quantity: int,
    ):
        print(f"Placing SELL order: {symbol} x{quantity}")

        # SAFETY: block SELL on CNC holdings — only MIS positions can close
        if not self._check_safety_sell(kite, symbol):
            return None

        order_id = kite.place_order(
            symbol=symbol,
            exchange=exchange,
            transaction_type='SELL',
            quantity=quantity,
            order_type='MARKET',
            product='MIS',
            variety='regular',
        )

        if not order_id:
            print(f"SELL order failed for {symbol}")
            return None

        time.sleep(config.ORDER_CONFIRMATION_WAIT_SECONDS)
        order = kite.get_order_status(order_id)

        if order and order.get('status') == 'COMPLETE':
            avg_price = order.get('average_price', 0) or 0
            filled_qty = order.get('filled_quantity', quantity) or quantity
            print(f"SELL confirmed: {symbol} x{filled_qty} @ ₹{avg_price}")
            return {
                'order_id': order_id,
                'status': 'COMPLETE',
                'price': avg_price,
                'quantity': filled_qty,
                'value': avg_price * filled_qty,
            }
        else:
            status = order.get('status', 'UNKNOWN') if order else 'UNKNOWN'
            print(f"SELL order status: {status} for {symbol}")
            return None

    def place_short_order(
        self,
        kite: KiteClient,
        symbol: str,
        exchange: str,
        quantity: int,
    ):
        """Open a NEW intraday short via MIS SELL. No safety lock (no long needed)."""
        print(f"Placing SHORT order: {symbol} x{quantity}")
        order_id = kite.place_order(
            symbol=symbol,
            exchange=exchange,
            transaction_type='SELL',
            quantity=quantity,
            order_type='MARKET',
            product='MIS',
            variety='regular',
        )
        if not order_id:
            print(f"SHORT order failed for {symbol}")
            return None

        time.sleep(config.ORDER_CONFIRMATION_WAIT_SECONDS)
        order = kite.get_order_status(order_id)
        if order and order.get('status') == 'COMPLETE':
            avg_price = order.get('average_price', 0) or 0
            filled_qty = order.get('filled_quantity', quantity) or quantity
            print(f"SHORT confirmed: {symbol} x{filled_qty} @ ₹{avg_price}")
            return {
                'order_id': order_id,
                'status': 'COMPLETE',
                'price': avg_price,
                'quantity': filled_qty,
                'value': avg_price * filled_qty,
            }
        status = order.get('status', 'UNKNOWN') if order else 'UNKNOWN'
        print(f"SHORT order status: {status} for {symbol}")
        return None

    def cover_short_order(
        self,
        kite: KiteClient,
        symbol: str,
        exchange: str,
        quantity: int,
    ):
        """Close (cover) an existing intraday short via MIS BUY."""
        print(f"Covering SHORT: {symbol} x{quantity}")
        order_id = kite.place_order(
            symbol=symbol,
            exchange=exchange,
            transaction_type='BUY',
            quantity=quantity,
            order_type='MARKET',
            product='MIS',
            variety='regular',
        )
        if not order_id:
            print(f"COVER order failed for {symbol}")
            return None

        time.sleep(config.ORDER_CONFIRMATION_WAIT_SECONDS)
        order = kite.get_order_status(order_id)
        if order and order.get('status') == 'COMPLETE':
            avg_price = order.get('average_price', 0) or 0
            filled_qty = order.get('filled_quantity', quantity) or quantity
            print(f"COVER confirmed: {symbol} x{filled_qty} @ ₹{avg_price}")
            return {
                'order_id': order_id,
                'status': 'COMPLETE',
                'price': avg_price,
                'quantity': filled_qty,
                'value': avg_price * filled_qty,
            }
        status = order.get('status', 'UNKNOWN') if order else 'UNKNOWN'
        print(f"COVER order status: {status} for {symbol}")
        return None

    def square_off_all(self, kite: KiteClient, open_trades: list) -> None:
        # SHORTs must be bought back (cover), not sold again — selling an
        # open short here would place a second, wrong-direction real order.
        print(f"Squaring off {len(open_trades)} open positions")
        for trade in open_trades:
            closer = (self.cover_short_order if trade.get('position_type') == 'SHORT'
                      else self.place_sell_order)
            closer(
                kite,
                trade['symbol'],
                trade['exchange'],
                trade['quantity'],
            )
            time.sleep(1)
