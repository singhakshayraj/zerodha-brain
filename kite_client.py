import csv
import io
import time
from datetime import datetime
from typing import Optional

import requests

import config


class TokenExpiredError(Exception):
    pass


class KiteAPIError(Exception):
    pass


class KiteClient:
    def __init__(self, token: str):
        self.token = token.strip()
        # Use kite.zerodha.com/oms not api.kite.trade
        self.base_url = 'https://kite.zerodha.com/oms'
        self.session = requests.Session()

        # Token must be in BOTH Authorization and Cookie
        self.session.headers.update({
            'Authorization': f'enctoken {self.token}',
            'Cookie': f'enctoken={self.token}',
            'X-Kite-Version': '3',
            'Content-Type': 'application/x-www-form-urlencoded',
        })

        print(f"[kite] Initialized with base URL: {self.base_url}")

        self._instrument_cache = {}
        self._instrument_map = {}
        self._instrument_cache_date = None

    # --- CORE HTTP ---

    def _get(self, path: str, params: dict = None, raw: bool = False):
        url = f"{self.base_url}{path}"
        print(f"[kite] GET {url}")

        for attempt in range(1, config.MAX_RETRIES + 1):
            try:
                response = self.session.get(url, params=params, timeout=10)

                if response.status_code == 403:
                    raise TokenExpiredError("Token expired")

                if response.status_code == 400:
                    body = response.text
                    print(f"[kite] error {attempt}: 400 body={body[:300]}")
                    if 'TokenException' in body or 'tokenexception' in body.lower():
                        raise TokenExpiredError("Token expired, needs refresh")
                    # 400 = bad request, not transient. Do not retry.
                    raise KiteAPIError(f"400 on {path}: {body[:200]}")

                if response.status_code != 200:
                    print(f"[kite] error {attempt}: {response.status_code} body={response.text[:200]}")
                    if attempt == config.MAX_RETRIES:
                        raise KiteAPIError(f"{response.status_code} on {path}")
                    time.sleep(2)
                    continue

                if raw:
                    return response.text

                data = response.json()
                if data.get('status') != 'success':
                    raise KiteAPIError(f"API error: {data.get('message')}")

                return data.get('data', data)

            except (TokenExpiredError, KiteAPIError):
                raise
            except Exception as e:
                if attempt == config.MAX_RETRIES:
                    raise
                print(f"[kite] GET retry {attempt}: {e}")
                time.sleep(2)

    def _post(self, path: str, data: dict = None):
        url = f"{self.base_url}{path}"

        if data:
            print(f"[kite] POST {url} payload={data}")

        for attempt in range(1, config.MAX_RETRIES + 1):
            try:
                response = self.session.post(url, data=data, timeout=10)

                if response.status_code == 403:
                    raise TokenExpiredError("Token expired")

                if response.status_code != 200:
                    print(f"[kite] POST error {attempt}: {response.status_code} body={response.text[:200]}")
                    if attempt == config.MAX_RETRIES:
                        raise KiteAPIError(f"{response.status_code} on {path}")
                    time.sleep(2)
                    continue

                data_resp = response.json()
                if data_resp.get('status') != 'success':
                    raise KiteAPIError(f"API error: {data_resp.get('message')}")

                return data_resp.get('data', data_resp)

            except (TokenExpiredError, KiteAPIError):
                raise
            except Exception as e:
                if attempt == config.MAX_RETRIES:
                    raise
                print(f"[kite] POST retry {attempt}: {e}")
                time.sleep(2)

    def _delete(self, path: str):
        url = f"{self.base_url}{path}"
        resp = self.session.delete(url, timeout=10)
        if resp.status_code == 403:
            raise TokenExpiredError(f"403 from Kite: {resp.text}")
        if resp.status_code >= 400:
            raise KiteAPIError(f"{resp.status_code} on DELETE {path}: {resp.text}")
        return resp.json().get('data')

    # --- USER ---

    def get_profile(self) -> dict:
        return self._get('/user/profile') or {}

    def get_funds(self) -> dict:
        return self._get('/user/margins') or {}

    # --- PORTFOLIO ---

    def get_holdings(self) -> list:
        return self._get('/portfolio/holdings') or []

    def get_positions(self) -> dict:
        data = self._get('/portfolio/positions')
        return data if data else {'day': [], 'net': []}

    # --- MARKET DATA ---

    def get_quote(self, symbols: list) -> dict:
        time.sleep(config.QUOTE_REQUEST_DELAY_MS / 1000.0)
        params = [('i', s) for s in symbols]
        return self._get('/quote', params=params) or {}

    def get_ltp(self, symbols: list) -> dict:
        time.sleep(config.QUOTE_REQUEST_DELAY_MS / 1000.0)
        params = [('i', s) for s in symbols]
        return self._get('/quote/ltp', params=params) or {}

    def get_historical_data(self, instrument_token: int, interval: str, from_date: str, to_date: str) -> list:
        path = f'/instruments/historical/{instrument_token}/{interval}'
        data = self._get(path, params={'from': from_date, 'to': to_date}) or {}
        candles_raw = data.get('candles', []) if isinstance(data, dict) else []
        out = []
        for c in candles_raw:
            if len(c) >= 6:
                out.append({
                    'timestamp': c[0],
                    'open': c[1],
                    'high': c[2],
                    'low': c[3],
                    'close': c[4],
                    'volume': c[5],
                })
        return out

    def get_instruments(self) -> list:
        # Instruments endpoint not available on kite.zerodha.com/oms
        # Tokens fetched from quote responses instead
        print("[kite] Instruments endpoint not available on OMS")
        return []

    def get_instrument_token(self, symbol: str):
        if not self._instrument_map:
            try:
                self.get_instruments()
            except Exception as e:
                print(f"[kite.get_instrument_token] failed loading instruments: {e}")
                return None
        return self._instrument_map.get(symbol)

    # --- ORDERS ---

    def place_order(
        self,
        symbol: str,
        exchange: str = 'NSE',
        transaction_type: str = 'BUY',
        quantity: int = 1,
        order_type: str = 'MARKET',
        product: str = 'MIS',
    ):
        try:
            # Strip exchange prefix if caller passed "NSE:SYMBOL"
            if ':' in symbol:
                exchange, symbol = symbol.split(':', 1)
            tradingsymbol = symbol.replace('NSE:', '').replace('BSE:', '')
            exchange = (exchange or 'NSE').upper()

            # TEMP: AMO test — ridiculously low LIMIT price, won't fill
            payload = {
                'variety':          'amo',
                'tradingsymbol':    tradingsymbol,
                'exchange':         exchange,
                'transaction_type': transaction_type,
                'order_type':       'LIMIT',
                'quantity':         str(quantity),
                'product':          'CNC',
                'validity':         'DAY',
                'price':            '1.00',
            }
            print(f"[kite] POST /orders/amo payload: {payload}")
            res = self._post('/orders/amo', data=payload) or {}
            return res.get('order_id')
        except Exception as e:
            print(f"[kite.place_order] error for {symbol}: {e}")
            return None

    def get_order_status(self, order_id: str):
        try:
            res = self._get(f'/orders/{order_id}')
            if isinstance(res, list) and len(res) > 0:
                return res[-1]
            return res
        except Exception as e:
            print(f"[kite.get_order_status] error for {order_id}: {e}")
            return None

    def cancel_order(self, order_id: str) -> bool:
        try:
            self._delete(f'/orders/regular/{order_id}')
            return True
        except Exception as e:
            print(f"[kite.cancel_order] error for {order_id}: {e}")
            return False

    def get_orders(self) -> list:
        try:
            return self._get('/orders') or []
        except Exception as e:
            print(f"[kite.get_orders] error: {e}")
            return []
