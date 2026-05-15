from datetime import datetime

import pytz

import config
from trading_principles import TradingPrinciples

IST = pytz.timezone('Asia/Kolkata')


class RiskManager:

    def __init__(self):
        self.consecutive_losses = 0

    def calculate_position_size(
        self,
        capital: float,
        live_price: float,
        confidence: int,
        stop_loss_price: float,
        historical_win_rate: float = None,
        historical_avg_win: float = None,
        historical_avg_loss: float = None,
    ) -> int:
        try:
            qty = TradingPrinciples.calculate_position_size(
                capital=capital,
                entry_price=live_price,
                stop_loss_price=stop_loss_price,
                win_rate=historical_win_rate if historical_win_rate else 0.50,
                avg_win=historical_avg_win if historical_avg_win else 100,
                avg_loss=historical_avg_loss if historical_avg_loss else 100,
                slippage_percent=0.005,
                commission_percent=0.001,
            )

            if qty * live_price < config.MIN_TRADE_VALUE:
                return 0

            return qty
        except Exception as e:
            print(f"[risk_manager.calculate_position_size] error: {e}")
            return 0

    def check_session_limits(self, session_stats: dict, session_config: dict) -> dict:
        try:
            capital = session_config['capitalDeployed']
            max_loss_percent = session_config['maxLossPercent']
            max_profit_amount = capital * session_config['maxProfitPercent'] / 100

            total_pnl = session_stats['total_pnl']
            trades_executed = session_stats['trades_executed']
            max_trades = session_config['maxTrades']

            consecutive_losses = session_stats.get(
                'consecutive_losses', self.consecutive_losses
            )

            check = TradingPrinciples.should_continue_trading(
                current_session_pnl=total_pnl,
                session_capital=capital,
                max_loss_percent=max_loss_percent,
                consecutive_losses=consecutive_losses,
                max_consecutive_losses=3,
            )

            if not check['should_continue']:
                return {'can_trade': False, 'reason': check['reason']}

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

            return {'can_trade': True, 'reason': check['reason']}
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
        m = now_ist.hour * 60 + now_ist.minute
        if m < 9 * 60 + 15:
            return 'PRE_MARKET'
        if m < 9 * 60 + 30:
            return 'OPENING'
        if m < 11 * 60 + 30:
            return 'MORNING'
        if m < 13 * 60:
            return 'MIDDAY'
        if m < 15 * 60:
            return 'AFTERNOON'
        if m < 15 * 60 + 15:
            return 'PRE_CLOSE'
        if m <= 15 * 60 + 30:
            return 'CLOSING'
        return 'AFTER_MARKET'
