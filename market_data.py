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
        self._instrument_cache = {}
        self.cache_ttl_seconds = 60
        self.candle_cache_ttl_seconds = 900

    def _now(self) -> datetime:
        return datetime.now(IST)

    def get_instrument_token(self, symbol: str):
        if symbol in self._instrument_cache:
            return self._instrument_cache[symbol]
        return None

    def get_candles(self, symbol: str, interval: str = '15minute', days: int = 5) -> list:
        try:
            key = f'{symbol}_{interval}'
            cached = self.candle_cache.get(key)
            now = self._now()
            if cached and (now - cached['fetched_at']).total_seconds() < self.candle_cache_ttl_seconds:
                return cached['data']

            instrument_token = self._instrument_cache.get(symbol)
            if not instrument_token:
                quotes = self.get_live_quote([symbol])
                q = quotes.get(symbol) if quotes else None
                instrument_token = q.get('instrument_token') if q else None

            if not instrument_token:
                print(f"[market_data.get_candles] no instrument token for {symbol}")
                return []

            self._instrument_cache[symbol] = instrument_token

            candles = self._get_historical(instrument_token, interval, days)
            self.candle_cache[key] = {'data': candles, 'fetched_at': now}
            return candles
        except Exception as e:
            print(f"[market_data.get_candles] error for {symbol}: {e}")
            return []

    def _get_historical(self, token: int, interval: str, days: int) -> list:
        now = self._now()

        if interval == '5minute':
            from_dt = now - timedelta(days=3)
        elif interval == '15minute':
            from_dt = now - timedelta(days=5)
        else:
            from_dt = now - timedelta(days=20)

        from_date = from_dt.strftime('%Y-%m-%d %H:%M:%S')
        to_date = now.strftime('%Y-%m-%d %H:%M:%S')

        try:
            result = self.kite._get(
                f'/instruments/historical/{token}/{interval}',
                params={'from': from_date, 'to': to_date},
            )

            candles_raw = result.get('candles', []) if isinstance(result, dict) else []
            return [
                {
                    'timestamp': c[0],
                    'open': c[1],
                    'high': c[2],
                    'low': c[3],
                    'close': c[4],
                    'volume': c[5],
                }
                for c in candles_raw
                if len(c) >= 6
            ]
        except Exception as e:
            print(f"[market_data._get_historical] failed: {e}")
            return []

    def get_live_quote(self, symbols) -> dict:
        """
        Fetch live quotes for one or more symbols.

        Zerodha requires: ?i=NSE:STOCK1&i=NSE:STOCK2
        NOT: ?symbols=['NSE:STOCK1', 'NSE:STOCK2']
        """
        try:
            # Accept str for backward compat — wrap as list
            if isinstance(symbols, str):
                symbols = [symbols]

            if not symbols:
                return {}

            now = self._now()
            quotes = {}
            uncached = []

            # Serve from cache where possible
            for s in symbols:
                cached = self.quote_cache.get(s)
                if cached and (now - cached['fetched_at']).total_seconds() < self.cache_ttl_seconds:
                    quotes[s] = cached['data']
                else:
                    uncached.append(s)

            if uncached:
                query_string = '&'.join(f'i={sym}' for sym in uncached)
                path = f'/quote?{query_string}'

                print(f"[market_data] Fetching quotes: {uncached}")
                result = self.kite._get(path) or {}

                for sym in uncached:
                    raw = result.get(sym)
                    if not raw:
                        continue
                    ohlc = raw.get('ohlc') or {}
                    depth = raw.get('depth') or {}
                    bid_arr = depth.get('buy') or []
                    ask_arr = depth.get('sell') or []
                    bid = bid_arr[0].get('price', 0) if bid_arr else 0
                    ask = ask_arr[0].get('price', 0) if ask_arr else 0

                    mapped = {
                        'price': raw.get('last_price', 0),
                        'last_price': raw.get('last_price', 0),
                        'high': raw.get('high') or ohlc.get('high', 0),
                        'low': raw.get('low') or ohlc.get('low', 0),
                        'close': raw.get('close') or ohlc.get('close', 0),
                        'prev_close': ohlc.get('close', 0),
                        'volume': raw.get('volume', 0),
                        'bid': bid,
                        'ask': ask,
                        'instrument_token': raw.get('instrument_token', 0),
                        'ohlc': ohlc,
                    }
                    quotes[sym] = mapped
                    self.quote_cache[sym] = {'data': mapped, 'fetched_at': now}

            print(f"[market_data] Got quotes for {len(quotes)} symbols")
            return quotes

        except Exception as e:
            print(f"[market_data.get_live_quote] error: {e}")
            return {}

    def get_live_quotes_batch(self, symbols: list) -> dict:
        # Kept for compatibility; delegates to chunked get_live_quote
        out = {}
        try:
            batch_size = config.MAX_SYMBOLS_PER_QUOTE
            for i in range(0, len(symbols), batch_size):
                batch = symbols[i:i + batch_size]
                out.update(self.get_live_quote(batch))
            return out
        except Exception as e:
            print(f"[market_data.get_live_quotes_batch] error: {e}")
            return out

    def get_nifty_level(self) -> dict:
        try:
            quotes = self.get_live_quote(['NSE:NIFTY 50'])
            q = quotes.get('NSE:NIFTY 50') or {}
            last_price = q.get('price') or q.get('last_price') or 0
            prev_close = q.get('prev_close') or 0
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
