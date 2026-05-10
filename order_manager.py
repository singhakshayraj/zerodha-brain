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

    def place_sell_order(
        self,
        kite: KiteClient,
        symbol: str,
        exchange: str,
        quantity: int,
    ):
        print(f"Placing SELL order: {symbol} x{quantity}")

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
