import time

import config
from kite_client import KiteClient


class OrderManager:

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

    def _has_intraday_long(self, kite: KiteClient, symbol: str, quantity: int) -> bool:
        try:
            positions = kite.get_positions() or {}
            net = positions.get('net', []) if isinstance(positions, dict) else []
            bare_symbol = symbol.replace('NSE:', '').replace('BSE:', '')
            for p in net:
                if (
                    p.get('tradingsymbol') == bare_symbol
                    and p.get('product') == 'MIS'
                    and (p.get('quantity') or 0) > 0
                ):
                    return True
            return False
        except Exception as e:
            print(f"[order_manager._has_intraday_long] error: {e}")
            return False

    def place_sell_order(
        self,
        kite: KiteClient,
        symbol: str,
        exchange: str,
        quantity: int,
    ):
        print(f"Placing SELL order: {symbol} x{quantity}")

        # SAFETY: never SELL unless an MIS long exists.
        # Prevents accidentally shorting from CNC holdings.
        if not self._has_intraday_long(kite, symbol, quantity):
            print(
                f"[SAFETY] Blocking SELL on {symbol} — "
                f"no intraday position to close. "
                f"Will not touch CNC holding."
            )
            return None

        order_id = kite.place_order(
            symbol=symbol,
            exchange=exchange,
            transaction_type='SELL',
            quantity=quantity,
            order_type='MARKET',
            product='MIS',
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

    def square_off_all(self, kite: KiteClient, open_trades: list) -> None:
        print(f"Squaring off {len(open_trades)} open positions")
        for trade in open_trades:
            self.place_sell_order(
                kite,
                trade['symbol'],
                trade['exchange'],
                trade['quantity'],
            )
            time.sleep(1)
