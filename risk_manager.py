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
        target_price: float = None,
        historical_win_rate: float = None,
        historical_avg_win: float = None,
        historical_avg_loss: float = None,
        n_trades: int = 0,
    ) -> int:
        try:
            stop_distance = abs(live_price - stop_loss_price)
            if stop_distance <= 0 or stop_distance > live_price * 0.5:
                print(
                    f"[risk] Invalid SL: price=₹{live_price:.2f} "
                    f"sl=₹{stop_loss_price:.2f} dist=₹{stop_distance:.2f} — skip"
                )
                return 0

            if live_price > capital:
                print(f"[risk] price ₹{live_price:.0f} exceeds capital ₹{capital:.0f} — skip")
                return 0

            # Try Kelly sizing when we have ≥10 historical trades and target price
            risk_amount = 0
            used_kelly = False
            if (
                historical_win_rate is not None
                and target_price is not None
                and n_trades >= 10
            ):
                reward_distance = abs(target_price - live_price)
                if reward_distance > 0:
                    b = reward_distance / stop_distance
                    w = historical_win_rate
                    kelly_f = w - (1 - w) / b
                    safe_f = max(0.0, kelly_f * 0.33)
                    kelly_risk = capital * safe_f
                    if kelly_risk >= 1:
                        risk_amount = kelly_risk
                        used_kelly = True
                        print(
                            f"[kelly] win={w:.1%} b={b:.2f} kelly={kelly_f:.4f} "
                            f"safe={safe_f:.4f} risk=₹{risk_amount:.2f}"
                        )

            if not used_kelly:
                risk_amount = capital * 0.01
                print(f"[kelly] Using fixed 1% sizing (n_trades={n_trades})")

            qty_risk = int(risk_amount / stop_distance)
            qty_max = int((capital * 0.15) / live_price)
            qty = max(1, min(qty_risk, qty_max if qty_max > 0 else 1))
            print(
                f"[risk] qty={qty} (risk_amt=₹{risk_amount:.0f} "
                f"sl_dist=₹{stop_distance:.2f} cap=₹{capital:.0f})"
            )
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
