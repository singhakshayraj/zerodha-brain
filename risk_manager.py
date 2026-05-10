from datetime import datetime

import pytz

import config

IST = pytz.timezone('Asia/Kolkata')


class RiskManager:

    def calculate_position_size(
        self,
        capital: float,
        live_price: float,
        confidence: int,
        stop_loss_price: float,
    ) -> int:
        try:
            risk_amount = capital * (config.MAX_RISK_PER_TRADE_PERCENT / 100)
            stop_distance = live_price - stop_loss_price
            if stop_distance <= 0:
                return 0

            quantity = risk_amount / stop_distance

            if confidence >= 80:
                quantity *= 1.2
            elif confidence < 70:
                quantity *= 0.8

            max_quantity = (capital * config.MAX_POSITION_SIZE_PERCENT / 100) / live_price
            quantity = min(quantity, max_quantity)

            if quantity * live_price < config.MIN_TRADE_VALUE:
                return 0

            return max(1, int(quantity))
        except Exception as e:
            print(f"[risk_manager.calculate_position_size] error: {e}")
            return 0

    def check_session_limits(self, session_stats: dict, session_config: dict) -> dict:
        try:
            capital = session_config['capitalDeployed']
            max_loss_amount = capital * session_config['maxLossPercent'] / 100
            max_profit_amount = capital * session_config['maxProfitPercent'] / 100

            total_pnl = session_stats['total_pnl']
            trades_executed = session_stats['trades_executed']
            max_trades = session_config['maxTrades']

            if total_pnl <= -max_loss_amount:
                return {
                    'can_trade': False,
                    'reason': f'MAX_LOSS_HIT: P&L ₹{total_pnl:.2f} '
                              f'exceeded limit ₹{-max_loss_amount:.2f}',
                }

            if total_pnl >= max_profit_amount:
                return {
                    'can_trade': False,
                    'reason': f'MAX_PROFIT_HIT: P&L ₹{total_pnl:.2f} '
                              f'reached target ₹{max_profit_amount:.2f}',
                }

            if trades_executed >= max_trades:
                return {
                    'can_trade': False,
                    'reason': f'MAX_TRADES_HIT: {trades_executed}/{max_trades} trades used',
                }

            if not self.is_market_open():
                return {'can_trade': False, 'reason': 'MARKET_CLOSED'}

            return {'can_trade': True, 'reason': None}
        except Exception as e:
            print(f"[risk_manager.check_session_limits] error: {e}")
            return {'can_trade': False, 'reason': f'ERROR: {e}'}

    def is_market_open(self) -> bool:
        now_ist = datetime.now(IST)

        if now_ist.weekday() > 4:
            return False

        today_str = now_ist.strftime('%Y-%m-%d')
        if today_str in config.NSE_HOLIDAYS_2025:
            return False

        market_open = now_ist.replace(
            hour=config.MARKET_OPEN_HOUR,
            minute=config.MARKET_OPEN_MINUTE,
            second=0,
            microsecond=0,
        )
        market_close = now_ist.replace(
            hour=config.MARKET_CLOSE_HOUR,
            minute=config.MARKET_CLOSE_MINUTE,
            second=0,
            microsecond=0,
        )

        return market_open <= now_ist <= market_close

    def get_time_bucket(self) -> str:
        now_ist = datetime.now(IST)
        total_minutes = now_ist.hour * 60 + now_ist.minute
        if total_minutes < 10 * 60:
            return 'OPENING'
        if total_minutes < 12 * 60:
            return 'MORNING'
        if total_minutes < 14 * 60:
            return 'AFTERNOON'
        return 'CLOSING'
