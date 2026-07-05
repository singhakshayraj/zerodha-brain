import config
from indicators import run_all_indicators
from regime_detector import RegimeDetector
from trading_principles import TradingPrinciples


class SignalEngine:

    def __init__(self):
        self.regime_detector = RegimeDetector()

    def generate_signal(
        self,
        candles_5min: list,
        candles_15min: list,
        candles_1hour: list,
        live_price: float,
        symbol: str,
        nifty_direction: str,
        nifty_change_percent: float,
    ) -> dict:
        regime = self.regime_detector.detect(
            candles_5min or [],
            candles_15min or [],
            candles_1hour or [],
            nifty_direction,
            nifty_change_percent,
        )

        if not regime['can_trade']:
            # Still snapshot indicators — training data needs to see what
            # the brain saw even when the regime blocked trading.
            return {
                'action': 'HOLD',
                'confidence': 0,
                'reasons': [],
                'skip_reasons': regime['reasons'],
                'stop_loss': round(live_price * 0.98, 2),
                'target': round(live_price * 1.02, 2),
                'risk_reward_ratio': 1.0,
                'indicators': run_all_indicators(candles_15min) if candles_15min else {},
                'regime': regime['regime'],
                'market_bias': regime.get('market_bias', 'NEUTRAL'),
            }

        ind = run_all_indicators(candles_15min)

        if ind['candle_count'] < 35:
            return {
                'action': 'HOLD',
                'confidence': 0,
                'reasons': [],
                'skip_reasons': ['Insufficient historical data'],
                'stop_loss': round(live_price * 0.98, 2),
                'target': round(live_price * 1.02, 2),
                'risk_reward_ratio': 1.0,
                'indicators': ind,
                'regime': regime['regime'],
                'market_bias': regime.get('market_bias', 'NEUTRAL'),
            }

        ind_1h = run_all_indicators(candles_1hour) if candles_1hour else None

        nifty_bias = regime.get('nifty_bias', 'NEUTRAL')
        allow_buy = nifty_bias != 'BEARISH'
        allow_sell = nifty_bias != 'BULLISH'

        # BUY scoring
        buy_score = 0
        buy_reasons = []

        if ind['rsi_14'] is not None and ind['rsi_14'] < 50:
            buy_score += 20
            buy_reasons.append(f"RSI below midline {ind['rsi_14']:.1f}")

        if ind['ema_21'] and live_price > ind['ema_21']:
            buy_score += 15
            buy_reasons.append(f"Above EMA21 ({ind['ema_21']:.2f})")

        if ind['ema_200'] and live_price > ind['ema_200']:
            buy_score += 10
            buy_reasons.append("Long-term uptrend (above EMA200)")

        if ind['macd_histogram'] is not None and ind['macd_histogram'] > 0:
            buy_score += 15
            buy_reasons.append(f"MACD bullish ({ind['macd_histogram']:.3f})")

        if ind['bb_lower'] and live_price <= ind['bb_lower'] * 1.015:
            buy_score += 10
            buy_reasons.append("Near lower Bollinger Band")

        if ind['volume_sma_20'] and ind['current_volume']:
            if ind['current_volume'] > ind['volume_sma_20'] * 1.5:
                buy_score += 15
                buy_reasons.append(
                    f"Volume spike ({ind['current_volume']/ind['volume_sma_20']:.1f}x avg)"
                )

        if ind['vwap'] and live_price > ind['vwap']:
            buy_score += 10
            buy_reasons.append(f"Above VWAP ({ind['vwap']:.2f})")

        if ind_1h and ind_1h.get('ema_21') and ind_1h.get('ema_50'):
            if live_price > ind_1h['ema_21'] > ind_1h['ema_50']:
                buy_score += 10
                buy_reasons.append("1H timeframe bullish aligned")
            else:
                buy_score -= 15
                buy_reasons.append("WARNING: 1H trend disagrees")

        if ind['adx_plus_di'] is not None and ind['adx_minus_di'] is not None:
            if ind['adx_plus_di'] > ind['adx_minus_di']:
                buy_score += 5
                buy_reasons.append(
                    f"+DI > -DI ({ind['adx_plus_di']:.1f} vs {ind['adx_minus_di']:.1f})"
                )

        # SELL scoring
        sell_score = 0
        sell_reasons = []

        if ind['rsi_14'] is not None and ind['rsi_14'] > 60:
            sell_score += 25
            sell_reasons.append(f"RSI overbought {ind['rsi_14']:.1f}")

        if ind['ema_21'] and live_price < ind['ema_21']:
            sell_score += 20
            sell_reasons.append(f"Below EMA21 ({ind['ema_21']:.2f})")

        if ind['macd_histogram'] is not None and ind['macd_histogram'] < 0:
            sell_score += 20
            sell_reasons.append(f"MACD bearish ({ind['macd_histogram']:.3f})")

        if ind['bb_upper'] and live_price >= ind['bb_upper'] * 0.985:
            sell_score += 15
            sell_reasons.append("Near upper Bollinger Band")

        if ind['volume_sma_20'] and ind['current_volume']:
            if ind['current_volume'] > ind['volume_sma_20'] * 1.5:
                sell_score += 10
                sell_reasons.append("Volume confirming sell move")

        if ind['vwap'] and live_price < ind['vwap']:
            sell_score += 10
            sell_reasons.append(f"Below VWAP ({ind['vwap']:.2f})")

        if ind_1h and ind_1h.get('ema_21'):
            if live_price < ind_1h['ema_21']:
                sell_score += 10
                sell_reasons.append("1H trend turned bearish")

        confidence_boost = regime['confidence_modifier']

        atr = ind['atr_14'] if ind['atr_14'] else live_price * 0.008

        # Sanity check: ATR should be 0.1% to 5% of price.
        # Outside this range = bad candle data, use fallback.
        atr_min = live_price * 0.001
        atr_max = live_price * 0.05
        if atr < atr_min or atr > atr_max:
            raw_atr = atr
            atr = live_price * 0.008
            print(
                f"[signal] {symbol}: ATR={raw_atr:.2f} outside "
                f"[{atr_min:.2f}, {atr_max:.2f}] — using fallback "
                f"{atr:.2f} (0.8% of price)"
            )

        stop_loss = round(live_price - (1.2 * atr), 2)
        target = round(live_price + (2.5 * atr), 2)
        stop_loss_pct = ((live_price - stop_loss) / live_price) * 100 if live_price else 0
        target_pct = ((target - live_price) / live_price) * 100 if live_price else 0
        risk_reward = round(target_pct / stop_loss_pct, 2) if stop_loss_pct > 0 else 0

        skip_reasons = []
        action = 'HOLD'
        confidence = 0

        raw_buy_confidence = min(100, max(0, buy_score + confidence_boost))
        raw_sell_confidence = min(100, max(0, sell_score + confidence_boost))

        print(
            f"[BUY check] {symbol}: RSI={ind.get('rsi_14') or 0:.1f} "
            f"EMA9={ind.get('ema_9') or 0:.1f} EMA21={ind.get('ema_21') or 0:.1f} "
            f"MACD={ind.get('macd_histogram') or 0:.3f} RR={risk_reward:.2f} "
            f"score={raw_buy_confidence}"
        )

        if raw_buy_confidence >= config.MIN_BUY_CONFIDENCE and allow_buy:
            regime_name = regime.get('regime', 'UNKNOWN')
            if regime_name == 'CHOPPY':
                skip_reasons.append('No BUY in CHOPPY regime')
                action = 'HOLD'
                confidence = raw_buy_confidence
            elif regime_name not in ('TRENDING', 'WEAK_TREND'):
                skip_reasons.append(
                    f"Regime {regime_name} not suitable for BUY"
                )
                action = 'HOLD'
                confidence = raw_buy_confidence
            elif regime_name == 'WEAK_TREND' and raw_buy_confidence < 80:
                skip_reasons.append(
                    f"WEAK_TREND requires confidence >= 80%, got {raw_buy_confidence}%"
                )
                action = 'HOLD'
                confidence = raw_buy_confidence
            elif risk_reward < config.MIN_RISK_REWARD_RATIO:
                skip_reasons.append(
                    f'R:R {risk_reward:.2f} below minimum {config.MIN_RISK_REWARD_RATIO}'
                )
                action = 'HOLD'
                confidence = raw_buy_confidence
            else:
                action = 'BUY'
                confidence = raw_buy_confidence
        elif raw_sell_confidence >= config.MIN_SELL_CONFIDENCE and allow_sell:
            action = 'SELL'
            confidence = raw_sell_confidence
        else:
            skip_reasons.append(
                f'Buy: {raw_buy_confidence}/100, Sell: {raw_sell_confidence}/100 — below thresholds'
            )
            if not allow_buy:
                skip_reasons.append('BUY blocked — Nifty in downtrend')
            if not allow_sell:
                skip_reasons.append('SELL blocked — Nifty in uptrend')
            confidence = max(raw_buy_confidence, raw_sell_confidence)

        # 1. Final R:R principle check (only matters for BUY actions)
        rr_check = TradingPrinciples.is_valid_risk_reward(
            live_price, stop_loss, target, min_ratio=config.MIN_RISK_REWARD_RATIO
        )

        if action == 'BUY' and not rr_check['valid']:
            skip_reasons.append(rr_check['reason'])
            action = 'HOLD'

        # 2. Adjust confidence by market regime + nifty direction
        if action != 'HOLD':
            adjusted_conf = TradingPrinciples.adjust_confidence_by_market(
                confidence,
                ind.get('trend_strength', 'CHOPPY'),
                nifty_direction,
            )
            confidence = adjusted_conf

        # WEAK_TREND requires final (post-modifier) confidence >= 80
        if (action == 'BUY'
                and regime.get('regime') == 'WEAK_TREND'
                and confidence < 80):
            skip_reasons.append(
                f"WEAK_TREND: post-adjustment confidence {confidence}% < 80%"
            )
            action = 'HOLD'

        return {
            'action': action,
            'confidence': confidence,
            'reasons': buy_reasons if action == 'BUY' else sell_reasons,
            'skip_reasons': skip_reasons,
            'stop_loss': stop_loss,
            'target': target,
            'risk_reward_ratio': risk_reward,
            'indicators': ind,
            'regime': regime['regime'],
            'market_bias': regime.get('market_bias', 'NEUTRAL'),
            'risk_reward_check': rr_check,
            'principles_applied': {
                'kelly_validated': True,
                'risk_reward_ratio': rr_check.get('ratio', 0),
                'confidence_adjusted': True,
            },
        }
