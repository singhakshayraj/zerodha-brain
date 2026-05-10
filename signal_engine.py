import config
from indicators import run_all_indicators


class SignalEngine:

    def generate_signal(self, candles: list, live_price: float, symbol: str) -> dict:
        ind = run_all_indicators(candles)

        if ind['candle_count'] < 35:
            return {
                'action': 'HOLD',
                'confidence': 0,
                'reasons': [],
                'skip_reasons': ['Insufficient historical data'],
                'stop_loss': 0,
                'target': 0,
                'risk_reward_ratio': 0,
                'indicators': ind,
            }

        # BUY scoring
        buy_score = 0
        buy_reasons = []

        if ind['rsi_14'] is not None and ind['rsi_14'] < 35:
            buy_score += 20
            buy_reasons.append(f"RSI oversold at {ind['rsi_14']}")

        if ind['ema_21'] and live_price > ind['ema_21']:
            buy_score += 15
            buy_reasons.append(f"Price above EMA21 ({ind['ema_21']})")

        if ind['ema_200'] and live_price > ind['ema_200']:
            buy_score += 15
            buy_reasons.append("Price in long-term uptrend (above EMA200)")

        if ind['macd_histogram'] is not None and ind['macd_histogram'] > 0:
            buy_score += 15
            buy_reasons.append(f"MACD bullish histogram: {ind['macd_histogram']}")

        if ind['bb_lower'] and ind['bb_middle']:
            if live_price <= ind['bb_lower'] * 1.02:
                buy_score += 15
                buy_reasons.append("Price near lower Bollinger Band")

        if ind['volume_sma_20'] and ind['current_volume']:
            if ind['current_volume'] > ind['volume_sma_20'] * 1.5:
                buy_score += 20
                buy_reasons.append(
                    f"Volume spike: {ind['current_volume']:.0f} "
                    f"vs avg {ind['volume_sma_20']:.0f}"
                )

        # SELL scoring
        sell_score = 0
        sell_reasons = []

        if ind['rsi_14'] is not None and ind['rsi_14'] > 65:
            sell_score += 25
            sell_reasons.append(f"RSI overbought at {ind['rsi_14']}")

        if ind['ema_21'] and live_price < ind['ema_21']:
            sell_score += 20
            sell_reasons.append(f"Price below EMA21 ({ind['ema_21']})")

        if ind['macd_histogram'] is not None and ind['macd_histogram'] < 0:
            sell_score += 20
            sell_reasons.append(f"MACD bearish histogram: {ind['macd_histogram']}")

        if ind['bb_upper'] and ind['bb_middle']:
            if live_price >= ind['bb_upper'] * 0.98:
                sell_score += 20
                sell_reasons.append("Price near upper Bollinger Band")

        if ind['volume_sma_20'] and ind['current_volume']:
            if ind['current_volume'] > ind['volume_sma_20'] * 1.5:
                sell_score += 15
                sell_reasons.append("Volume confirming move")

        # Stop loss / target
        atr = ind['atr_14'] if ind['atr_14'] else live_price * 0.01
        stop_loss = round(live_price - (2 * atr), 2)
        target = round(live_price + (3 * atr), 2)
        stop_loss_pct = ((live_price - stop_loss) / live_price) * 100 if live_price else 0
        target_pct = ((target - live_price) / live_price) * 100 if live_price else 0
        risk_reward = round(target_pct / stop_loss_pct, 2) if stop_loss_pct > 0 else 0

        skip_reasons = []
        action = 'HOLD'
        confidence = 0

        if buy_score >= config.MIN_BUY_CONFIDENCE:
            if risk_reward < config.MIN_RISK_REWARD_RATIO:
                skip_reasons.append(
                    f"Risk/reward {risk_reward} below minimum "
                    f"{config.MIN_RISK_REWARD_RATIO}"
                )
                action = 'HOLD'
                confidence = buy_score
            else:
                action = 'BUY'
                confidence = min(100, buy_score)
        elif sell_score >= config.MIN_SELL_CONFIDENCE:
            action = 'SELL'
            confidence = min(100, sell_score)
        else:
            action = 'HOLD'
            skip_reasons.append(
                f"Buy score {buy_score}/100, "
                f"Sell score {sell_score}/100 — "
                f"neither above threshold"
            )
            confidence = max(buy_score, sell_score)

        return {
            'action': action,
            'confidence': confidence,
            'reasons': buy_reasons if action == 'BUY' else sell_reasons,
            'skip_reasons': skip_reasons,
            'stop_loss': stop_loss,
            'target': target,
            'risk_reward_ratio': risk_reward,
            'indicators': ind,
        }
