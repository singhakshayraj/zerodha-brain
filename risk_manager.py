import math
from datetime import datetime

import pytz

import config
from trading_principles import TradingPrinciples

IST = pytz.timezone('Asia/Kolkata')

BROKERAGE_PER_LEG = 20.0       # Rs20 per order (Zerodha flat MIS)
ROUND_TRIP_BROKERAGE = 40.0    # entry + exit
MAX_BROKERAGE_PCT = 0.01       # warn if brokerage > 1%


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
        symbol: str = 'STOCK',
    ) -> int:
        try:
            stop_distance = abs(live_price - stop_loss_price)
            if stop_distance <= 0:
                print(
                    f"[risk] {symbol}: SL invalid — stop_distance=0 "
                    f"(price={live_price:.2f} == stop={stop_loss_price:.2f})"
                )
                return 0
            if stop_distance > live_price * 0.5:
                print(
                    f"[risk] {symbol}: SL too wide — "
                    f"stop_dist={stop_distance:.2f} > 50% of price "
                    f"({live_price*0.5:.2f}). price={live_price:.2f} "
                    f"stop={stop_loss_price:.2f} — likely bad ATR"
                )
                return 0

            if live_price > capital:
                print(f"[risk] price ₹{live_price:.0f} exceeds capital ₹{capital:.0f} — skip")
                return 0

            # Try Kelly sizing when we have ≥10 historical trades and target price
            risk_amount = 0
            used_kelly = False
            kelly_risk_computed = 0.0
            kelly_f = 0.0
            b = 0.0
            w = historical_win_rate or 0.0
            if (
                historical_win_rate is not None
                and target_price is not None
                and n_trades >= 10
            ):
                reward_distance = abs(target_price - live_price)
                if reward_distance > 0:
                    b = reward_distance / stop_distance
                    kelly_f = w - (1 - w) / b
                    safe_f = max(0.0, kelly_f * 0.33)
                    kelly_risk_computed = capital * safe_f
                    if kelly_risk_computed >= 1:
                        risk_amount = kelly_risk_computed
                        used_kelly = True

            if used_kelly:
                print(
                    f"[kelly] DYNAMIC: win={w:.1%} b={b:.2f} f={kelly_f:.4f} "
                    f"-> risk=Rs{risk_amount:.0f}"
                )
            else:
                risk_amount = capital * 0.01
                reason = (
                    f"only {n_trades}/10 trades"
                    if n_trades < 10
                    else "kelly_risk<1"
                )
                print(
                    f"[kelly] FIXED 1%: ({reason}) -> risk=Rs{risk_amount:.0f}"
                )

            max_position_pct = config.MAX_POSITION_PERCENT
            min_position_value = config.MIN_POSITION_VALUE

            qty_risk = int(risk_amount / stop_distance)
            qty_max = int((capital * max_position_pct) / live_price)
            qty = max(1, min(qty_risk, qty_max if qty_max > 0 else 1))
            original_qty = qty

            # Hard cap by absolute position value (max_position_pct of capital)
            max_value = capital * max_position_pct
            position_value = qty * live_price
            if position_value > max_value and qty > 1:
                capped = max(1, int(max_value / live_price))
                print(
                    f"[risk] Capped by value: ₹{position_value:.0f} > "
                    f"₹{max_value:.0f} → qty {qty}→{capped}"
                )
                qty = capped

            if qty != original_qty:
                print(
                    f"[size] {symbol}: value-capped {original_qty}->{qty} "
                    f"(max Rs{max_value:.0f})"
                )

            # Enforce minimum position value (brokerage economics)
            position_value = qty * live_price
            if position_value < min_position_value:
                min_qty = math.ceil(min_position_value / live_price)
                affordable_max = int(capital * max_position_pct / live_price)
                if min_qty <= affordable_max and (min_qty * live_price) <= capital:
                    print(
                        f"[size] {symbol}: Rs{position_value:.0f} below "
                        f"min Rs{min_position_value} → qty {qty}→{min_qty} "
                        f"(Rs{min_qty * live_price:.0f})"
                    )
                    qty = min_qty
                else:
                    print(
                        f"[size] {symbol}: WARNING cannot reach "
                        f"Rs{min_position_value} min within capital "
                        f"(max affordable qty={affordable_max} = "
                        f"Rs{affordable_max * live_price:.0f})"
                    )

            position_value = qty * live_price
            pct_of_capital = (position_value / capital * 100) if capital else 0
            print(
                f"[size] {symbol}: Rs{risk_amount:.0f}/Rs{live_price:.0f} = "
                f"qty={qty} (Rs{position_value:.0f} position, "
                f"{pct_of_capital:.1f}% of capital)"
            )

            print(
                f"[risk] qty={qty} (risk_amt=₹{risk_amount:.0f} "
                f"sl_dist=₹{stop_distance:.2f} cap=₹{capital:.0f})"
            )

            brokerage_pct = ROUND_TRIP_BROKERAGE / position_value if position_value else 0
            if brokerage_pct > MAX_BROKERAGE_PCT:
                min_pos = int(ROUND_TRIP_BROKERAGE / MAX_BROKERAGE_PCT)
                print(
                    f"[brokerage] WARNING {symbol}: "
                    f"Rs{position_value:.0f} position, "
                    f"Rs40 round-trip = {brokerage_pct*100:.1f}% cost "
                    f"(need Rs{min_pos}+ for <2% brokerage)"
                )
            else:
                print(
                    f"[brokerage] {symbol}: Rs40/Rs{position_value:.0f} = "
                    f"{brokerage_pct*100:.1f}% -- OK"
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
        if today_str in config.NSE_HOLIDAYS:
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
