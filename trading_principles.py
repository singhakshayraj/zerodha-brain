import math
from datetime import datetime

import pytz

IST = pytz.timezone('Asia/Kolkata')


class TradingPrinciples:
    """
    Implements mathematical principles from:
    - "Position Sizing" by Van Tharp
    - "The Intelligent Trader" by Alexander Elder
    - "Trading in the Zone" by Mark Douglas
    - "The Psychology of Trading" by Brett Steenbarger
    - "Flash Boys" by Michael Lewis
    """

    @staticmethod
    def kelly_fraction(
        win_rate: float,
        avg_win: float,
        avg_loss: float,
        safety_multiplier: float = 0.33,
    ) -> float:
        try:
            if win_rate <= 0 or win_rate > 1:
                return 0.01

            if avg_loss <= 0 or avg_win <= 0:
                return 0.01

            b = avg_win / avg_loss
            p = win_rate
            q = 1 - win_rate

            kelly_f = (b * p - q) / b
            safe_f = kelly_f * safety_multiplier
            result = max(0.01, min(safe_f, 0.25))

            print(
                f"[kelly] win_rate={win_rate:.2%}, b={b:.2f}, "
                f"kelly_f={kelly_f:.4f}, safe_f={safe_f:.4f}"
            )

            return result

        except Exception as e:
            print(f"[kelly] Error: {e}")
            return 0.01

    @staticmethod
    def is_valid_risk_reward(
        entry_price: float,
        stop_loss_price: float,
        target_price: float,
        min_ratio: float = 2.0,
    ) -> dict:
        try:
            risk = entry_price - stop_loss_price
            reward = target_price - entry_price

            if risk <= 0:
                return {
                    'valid': False,
                    'ratio': 0,
                    'reason': 'Stop loss above entry price',
                }

            if reward <= 0:
                return {
                    'valid': False,
                    'ratio': 0,
                    'reason': 'Target below entry price',
                }

            ratio = reward / risk

            return {
                'valid': ratio >= min_ratio,
                'ratio': round(ratio, 2),
                'reward': round(reward, 2),
                'risk': round(risk, 2),
                'reason': f"R:R {ratio:.2f} {'OK' if ratio >= min_ratio else f'below {min_ratio}'}",
            }

        except Exception as e:
            print(f"[risk_reward] Error: {e}")
            return {'valid': False, 'ratio': 0, 'reason': str(e)}

    @staticmethod
    def calculate_expectancy(
        win_rate: float,
        avg_win: float,
        avg_loss: float,
        commission_percent: float = 0.001,
    ) -> float:
        try:
            if win_rate <= 0 or win_rate > 1:
                return -1.0

            gross = (win_rate * avg_win) - ((1 - win_rate) * avg_loss)
            net = gross - commission_percent

            return round(net, 4)

        except Exception as e:
            print(f"[expectancy] Error: {e}")
            return -1.0

    @staticmethod
    def calculate_max_drawdown_capital(
        capital: float,
        acceptable_drawdown_percent: float = 15,
    ) -> float:
        try:
            return capital * (acceptable_drawdown_percent / 100)
        except Exception as e:
            print(f"[max_drawdown] Error: {e}")
            return capital * 0.15

    @staticmethod
    def calculate_position_size(
        capital: float,
        entry_price: float,
        stop_loss_price: float,
        win_rate: float,
        avg_win: float,
        avg_loss: float,
        slippage_percent: float = 0.005,
        commission_percent: float = 0.001,
    ) -> int:
        try:
            kelly_f = TradingPrinciples.kelly_fraction(
                win_rate, avg_win, avg_loss, safety_multiplier=0.33
            )

            risk_amount = capital * kelly_f
            adjusted_entry = entry_price * (1 + slippage_percent)
            stop_distance = adjusted_entry - stop_loss_price

            if stop_distance <= 0:
                print(f"[position_size] Invalid stop loss")
                return 0

            quantity = risk_amount / stop_distance

            max_per_trade = capital * 0.15 / entry_price
            min_per_trade = 1

            final_quantity = int(max(min_per_trade, min(quantity, max_per_trade)))

            print(
                f"[position_size] Kelly={kelly_f:.2%}, "
                f"risk={risk_amount:.0f}, qty={final_quantity}"
            )

            return final_quantity

        except Exception as e:
            print(f"[position_size] Error: {e}")
            return 0

    @staticmethod
    def should_continue_trading(
        current_session_pnl: float,
        session_capital: float,
        max_loss_percent: float = 5,
        consecutive_losses: int = 0,
        max_consecutive_losses: int = 2,
    ) -> dict:
        try:
            max_loss = session_capital * (max_loss_percent / 100)

            if current_session_pnl <= -max_loss:
                return {
                    'should_continue': False,
                    'reason': f'Max loss hit: ₹{current_session_pnl:.2f} '
                              f'vs limit ₹{-max_loss:.2f}',
                }

            if consecutive_losses >= max_consecutive_losses:
                return {
                    'should_continue': False,
                    'reason': f'{consecutive_losses} consecutive losses. '
                              f"Take a break. You're in tilt.",
                }

            return {
                'should_continue': True,
                'reason': f'Loss: ₹{current_session_pnl:.2f} '
                          f'(limit: ₹{-max_loss:.2f}), '
                          f'Consecutive losses: {consecutive_losses}',
            }

        except Exception as e:
            print(f"[circuit_breaker] Error: {e}")
            return {'should_continue': True, 'reason': str(e)}

    @staticmethod
    def adjust_confidence_by_market(
        base_confidence: int,
        market_regime: str,
        nifty_direction: str,
    ) -> int:
        try:
            adjusted = base_confidence

            if market_regime == 'CHOPPY':
                adjusted -= 20
                print(f"[confidence] Choppy market -20")
            elif market_regime == 'SIDEWAYS':
                adjusted -= 25
                print(f"[confidence] Sideways market -25")
            elif market_regime == 'WEAK_TREND' or market_regime == 'WEAK':
                adjusted -= 5
                print(f"[confidence] Weak trend -5")
            elif market_regime == 'TRENDING' or market_regime == 'STRONG':
                adjusted += 5
                print(f"[confidence] Trending market +5")

            if nifty_direction == 'BULLISH':
                adjusted += 5
                print(f"[confidence] Nifty bullish +5")
            elif nifty_direction == 'BEARISH':
                adjusted += 5
                print(f"[confidence] Nifty bearish +5")
            else:
                adjusted -= 5
                print(f"[confidence] Nifty neutral -5")

            result = max(0, min(adjusted, 100))
            print(f"[confidence] {base_confidence} -> {result}")

            return result

        except Exception as e:
            print(f"[adjust_confidence] Error: {e}")
            return base_confidence

    @staticmethod
    def estimate_slippage(
        stock_price: float,
        volatility: float = 1.0,
        quantity: int = 100,
        market_condition: str = 'normal',
    ) -> float:
        try:
            base_slippage_percent = 0.005
            volatility_factor = 1.0 + (volatility / 100.0)
            size_factor = 1.0 + (quantity / 1000.0)

            condition_factors = {
                'opening': 1.5,
                'closing': 1.2,
                'normal': 1.0,
                'lunch': 0.8,
            }

            condition_factor = condition_factors.get(market_condition, 1.0)

            total_slippage_percent = (
                base_slippage_percent
                * volatility_factor
                * size_factor
                * condition_factor
            )

            slippage_rupees = stock_price * total_slippage_percent
            max_slippage = stock_price * 0.05
            final_slippage = min(slippage_rupees, max_slippage)

            print(
                f"[slippage] Rs{stock_price} x {quantity} shares, "
                f"volatility {volatility:.1f}%, "
                f"condition={market_condition} "
                f"-> slippage Rs{final_slippage:.2f}"
            )

            return final_slippage

        except Exception as e:
            print(f"[slippage] Error: {e}")
            return stock_price * 0.005

    @staticmethod
    def is_tradeable_indian_stock(
        symbol: str,
        time: datetime = None,
        is_corporate_action: bool = False,
    ) -> dict:
        try:
            if is_corporate_action:
                return {
                    'tradeable': False,
                    'reason': f'{symbol} has corporate action — skip',
                }

            now = time if time is not None else datetime.now(IST)
            if now.tzinfo is None:
                now = IST.localize(now)
            else:
                now = now.astimezone(IST)

            minutes = now.hour * 60 + now.minute

            # 9:15-9:30 opening chaos
            if 9 * 60 + 15 <= minutes < 9 * 60 + 30:
                return {
                    'tradeable': False,
                    'reason': 'Opening 15 min (9:15-9:30) — chaotic, skip',
                }

            # 3:15-3:30 closing
            if 15 * 60 + 15 <= minutes < 15 * 60 + 30:
                return {
                    'tradeable': False,
                    'reason': 'Closing window (3:15-3:30) — skip',
                }

            return {'tradeable': True, 'reason': 'OK'}

        except Exception as e:
            print(f"[tradeable] Error: {e}")
            return {'tradeable': True, 'reason': str(e)}
