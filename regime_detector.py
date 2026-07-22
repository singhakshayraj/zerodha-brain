from datetime import datetime

import pytz

import config
from indicators import calculate_adx, get_candle_direction

IST = pytz.timezone('Asia/Kolkata')


class RegimeDetector:

    def __init__(self):
        self.market_bias = 'NEUTRAL'

    def detect(
        self,
        candles_5min: list,
        candles_15min: list,
        candles_1hour: list,
        nifty_direction: str,
        nifty_change_percent: float,
        now: datetime = None,
    ) -> dict:
        # `now` override lets a backtest/replay harness walk historical
        # timestamps through the same intraday-clock gates live trading
        # uses, instead of always reading the wall clock (gate #6 —
        # decision-fidelity replay needs the real code path, not a copy).
        now = now or datetime.now(IST)
        hour = now.hour
        minute = now.minute
        time_minutes = hour * 60 + minute

        # QA rehearsals run off-hours — pretend it's mid-morning so the
        # intraday clock gates (opening window, last-30-min, lunch) don't
        # block every synthetic signal.
        if config.QA_MODE:
            time_minutes = 11 * 60

        start_min = (
            config.MARKET_START_TRADING_HOUR * 60
            + config.MARKET_START_TRADING_MINUTE
        )
        no_entries_min = (
            config.MARKET_NO_NEW_ENTRIES_HOUR * 60
            + config.MARKET_NO_NEW_ENTRIES_MINUTE
        )

        # CHECK 1
        if time_minutes < start_min:
            return {
                'can_trade': False,
                'regime': 'BLOCKED',
                'confidence_modifier': 0,
                'market_bias': 'NEUTRAL',
                'nifty_bias': 'NEUTRAL',
                'reasons': ['Opening 15 minutes — waiting for market to stabilize'],
            }

        # CHECK 2
        if time_minutes > no_entries_min:
            return {
                'can_trade': False,
                'regime': 'BLOCKED',
                'confidence_modifier': 0,
                'market_bias': 'NEUTRAL',
                'nifty_bias': 'NEUTRAL',
                'reasons': ['Last 30 minutes — no new entries, exits only'],
            }

        # CHECK 3 — lunch
        lunch_start = config.LUNCH_START_HOUR * 60 + config.LUNCH_START_MINUTE
        lunch_end = config.LUNCH_END_HOUR * 60 + config.LUNCH_END_MINUTE
        in_lunch = lunch_start <= time_minutes <= lunch_end
        lunch_penalty = -15 if in_lunch else 0

        # CHECK 4 — ADX on 15min
        adx_data = calculate_adx(candles_15min) if candles_15min else None

        if adx_data is None:
            regime = 'UNKNOWN'
            adx_modifier = 0
        elif adx_data['adx'] > config.get_tunable('ADX_TRENDING_THRESHOLD'):
            regime = 'TRENDING'
            adx_modifier = 10
        elif adx_data['adx'] >= config.get_tunable('ADX_WEAK_THRESHOLD'):
            regime = 'WEAK_TREND'
            adx_modifier = 0
        else:
            return {
                'can_trade': False,
                'regime': 'CHOPPY',
                'confidence_modifier': 0,
                'market_bias': 'NEUTRAL',
                'nifty_bias': 'NEUTRAL',
                'reasons': [f'Market choppy — ADX {adx_data["adx"]:.1f} below 20'],
            }

        # CHECK 5 — Nifty
        nifty_modifier = 0
        nifty_reasons = []
        if nifty_change_percent <= -0.5:
            nifty_bias = 'BEARISH'
            nifty_modifier = -20
            nifty_reasons.append(
                f'Nifty falling {nifty_change_percent:.2f}% — only SELL signals valid'
            )
        elif nifty_change_percent >= 0.5:
            nifty_bias = 'BULLISH'
            nifty_modifier = 10
            nifty_reasons.append(
                f'Nifty rising {nifty_change_percent:.2f}% — BUY signals favoured'
            )
        else:
            nifty_bias = 'NEUTRAL'
            nifty_reasons.append('Nifty flat — neutral market')

        # CHECK 6 — multi timeframe
        dir_5min = get_candle_direction(candles_5min, 3) if candles_5min else 'NEUTRAL'
        dir_15min = get_candle_direction(candles_15min, 3) if candles_15min else 'NEUTRAL'
        dir_1hour = get_candle_direction(candles_1hour, 3) if candles_1hour else 'NEUTRAL'

        directions = [dir_5min, dir_15min, dir_1hour]
        bullish_count = directions.count('BULLISH')
        bearish_count = directions.count('BEARISH')

        if bullish_count >= 2:
            self.market_bias = 'BULLISH'
            timeframe_modifier = 10
        elif bearish_count >= 2:
            self.market_bias = 'BEARISH'
            timeframe_modifier = 10
        else:
            self.market_bias = 'NEUTRAL'
            timeframe_modifier = -10

        total_modifier = adx_modifier + nifty_modifier + timeframe_modifier + lunch_penalty

        all_reasons = nifty_reasons + [
            f'Market regime: {regime}',
            f'5min trend: {dir_5min}',
            f'15min trend: {dir_15min}',
            f'1hour trend: {dir_1hour}',
            f'Overall market bias: {self.market_bias}',
        ]
        if in_lunch:
            all_reasons.append('Lunch hours — confidence reduced')

        return {
            'can_trade': True,
            'regime': regime,
            'confidence_modifier': total_modifier,
            'market_bias': self.market_bias,
            'nifty_bias': nifty_bias,
            'reasons': all_reasons,
        }
