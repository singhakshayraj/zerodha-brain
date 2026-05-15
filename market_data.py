import time
from datetime import datetime, timedelta

import pytz

import config
from kite_client import KiteClient

INDEX_BLOCKLIST = ('NIFTY', 'SENSEX', 'BANKNIFTY', 'FINNIFTY')

IST = pytz.timezone('Asia/Kolkata')


class MarketData:
    def __init__(self, kite: KiteClient):
        self.kite = kite
        self.candle_cache = {}
        self.quote_cache = {}
        self._instrument_cache = {}
        self._holdings_cache = {}
        self._holdings_cache_time = 0.0
        self.cache_ttl_seconds = 60
        self.candle_cache_ttl_seconds = 900

    def refresh_holdings_cache(self) -> bool:
        """Fetch holdings once and cache prices + instrument_tokens."""
        try:
            holdings = self.kite.get_holdings() or []
            now = time.time()

            for h in holdings:
                tsym = h.get('tradingsymbol')
                if not tsym:
                    continue
                exch = h.get('exchange') or 'NSE'
                key = f"{exch}:{tsym}"

                price = h.get('last_price', 0) or 0
                token = h.get('instrument_token', 0) or 0

                self._holdings_cache[key] = {
                    'price': price,
                    'last_price': price,
                    'high': h.get('day_change', 0) or 0,
                    'low': price,
                    'close': h.get('close_price', 0) or 0,
                    'prev_close': h.get('close_price', 0) or 0,
                    'volume': h.get('volume', 0) or 0,
                    'bid': 0,
                    'ask': 0,
                    'instrument_token': token,
                    'ohlc': {'close': h.get('close_price', 0) or 0},
                }
                if token:
                    self._instrument_cache[key] = token

            self._holdings_cache_time = now
            print(f"[market_data] Cached {len(self._holdings_cache)} holdings")
            return True
        except Exception as e:
            print(f"[market_data] Holdings cache error: {e}")
            return False

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

    def _is_blocked_symbol(self, sym: str) -> bool:
        upper = sym.upper()
        return any(idx in upper for idx in INDEX_BLOCKLIST)

    def _rewrite_exchange(self, sym: str) -> str:
        # BSE: → NSE: for /quote (OMS quote uses NSE for most stocks)
        if sym.startswith('BSE:'):
            return 'NSE:' + sym[4:]
        return sym

    def get_live_quote(self, symbols) -> dict:
        """
        Return prices from holdings cache. No TTL check — caller
        is responsible for calling refresh_holdings_cache() first.
        """
        try:
            if isinstance(symbols, str):
                symbols = [symbols]

            if not symbols:
                return {}

            result = {}
            for sym in symbols:
                if sym in self._holdings_cache:
                    result[sym] = self._holdings_cache[sym]

            print(
                f"[market_data] Returning {len(result)} of "
                f"{len(symbols)} requested quotes"
            )
            return result

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
        # /quote disabled — no Nifty 50 access via OMS for retail
        # Return neutral context so regime detector treats market as SIDEWAYS
        try:
            last_price = 0
            prev_close = 0
            change_percent = 0.0

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
