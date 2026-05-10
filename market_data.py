from datetime import datetime, timedelta

import pytz

import config
from kite_client import KiteClient

IST = pytz.timezone('Asia/Kolkata')


class MarketData:
    def __init__(self, kite: KiteClient):
        self.kite = kite
        self.candle_cache = {}
        self.quote_cache = {}
        self.cache_ttl_seconds = 60
        self.candle_cache_ttl_seconds = 900

    def _now(self) -> datetime:
        return datetime.now(IST)

    def get_candles(self, symbol: str, interval: str = '15minute', days: int = 5) -> list:
        try:
            key = f'{symbol}_{interval}'
            cached = self.candle_cache.get(key)
            now = self._now()
            if cached and (now - cached['fetched_at']).total_seconds() < self.candle_cache_ttl_seconds:
                return cached['data']

            token = self.kite.get_instrument_token(symbol)
            if not token:
                print(f"[market_data.get_candles] no token for {symbol}")
                return []

            from_dt = now - timedelta(days=days)
            from_str = from_dt.strftime('%Y-%m-%d %H:%M:%S')
            to_str = now.strftime('%Y-%m-%d %H:%M:%S')

            candles = self.kite.get_historical_data(token, interval, from_str, to_str)
            self.candle_cache[key] = {'data': candles, 'fetched_at': now}
            return candles
        except Exception as e:
            print(f"[market_data.get_candles] error for {symbol}: {e}")
            return []

    def get_live_quote(self, symbol: str):
        try:
            now = self._now()
            cached = self.quote_cache.get(symbol)
            if cached and (now - cached['fetched_at']).total_seconds() < self.cache_ttl_seconds:
                return cached['data']
            res = self.kite.get_quote([symbol]) or {}
            data = res.get(symbol)
            self.quote_cache[symbol] = {'data': data, 'fetched_at': now}
            return data
        except Exception as e:
            print(f"[market_data.get_live_quote] error for {symbol}: {e}")
            return None

    def get_live_quotes_batch(self, symbols: list) -> dict:
        out = {}
        try:
            batch_size = config.MAX_SYMBOLS_PER_QUOTE
            for i in range(0, len(symbols), batch_size):
                batch = symbols[i:i + batch_size]
                try:
                    res = self.kite.get_quote(batch) or {}
                    out.update(res)
                except Exception as e:
                    print(f"[market_data.get_live_quotes_batch] batch error: {e}")
            return out
        except Exception as e:
            print(f"[market_data.get_live_quotes_batch] error: {e}")
            return out

    def get_nifty_level(self) -> dict:
        try:
            q = self.get_live_quote('NSE:NIFTY 50') or {}
            last_price = q.get('last_price') or 0
            ohlc = q.get('ohlc') or {}
            prev_close = ohlc.get('close') or 0
            change_percent = 0.0
            if prev_close:
                change_percent = ((last_price - prev_close) / prev_close) * 100

            if change_percent > 0.3:
                direction = 'BULLISH'
            elif change_percent < -0.3:
                direction = 'BEARISH'
            else:
                direction = 'SIDEWAYS'

            return {
                'level': last_price,
                'change_percent': change_percent,
                'direction': direction,
            }
        except Exception as e:
            print(f"[market_data.get_nifty_level] error: {e}")
            return {'level': 0, 'change_percent': 0, 'direction': 'SIDEWAYS'}

    def get_time_bucket(self) -> str:
        now = self._now()
        t = now.time()
        if t < datetime.strptime('10:00', '%H:%M').time():
            return 'OPENING'
        if t < datetime.strptime('12:00', '%H:%M').time():
            return 'MORNING'
        if t < datetime.strptime('14:00', '%H:%M').time():
            return 'AFTERNOON'
        return 'CLOSING'

    def clear_cache(self) -> None:
        self.candle_cache = {}
        self.quote_cache = {}
