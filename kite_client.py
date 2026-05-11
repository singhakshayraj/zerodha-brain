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
        self.token = token
        self.base_url = config.KITE_BASE_URL
        self.session = requests.Session()
        # Authorization only — no Content-Type globally (breaks GET requests)
        self.session.headers.update({
            'Authorization': f'enctoken {token}',
            'X-Kite-Version': '3',
        })
        self._instrument_cache = {}
        self._instrument_map = {}
        self._instrument_cache_date = None

    # --- CORE HTTP ---

    def _get(self, path: str, params=None, raw: bool = False):
        url = f"{self.base_url}{path}"
        last_err = None
        for attempt in range(config.MAX_RETRIES):
            try:
                resp = self.session.get(url, params=params, timeout=10)
                if resp.status_code == 403:
                    raise TokenExpiredError(f"403 from Kite: {resp.text}")
                if resp.status_code == 429:
                    print(f"[kite] 429 rate limit on {path}, sleeping 1s")
                    time.sleep(1)
                    continue
                if resp.status_code >= 400:
                    raise KiteAPIError(f"{resp.status_code} on {path}: {resp.text}")
                if raw:
                    return resp.text
                return resp.json().get('data')
            except TokenExpiredError:
                raise
            except KiteAPIError as e:
                last_err = e
                print(f"[kite] GET error attempt {attempt+1}: {e}")
                time.sleep(1)
            except requests.RequestException as e:
                last_err = e
                print(f"[kite] GET network error attempt {attempt+1}: {e}")
                time.sleep(2)
        raise KiteAPIError(f"GET max retries exceeded for {path}: {last_err}")

    def _post(self, path: str, data: dict = None):
        url = f"{self.base_url}{path}"
        last_err = None
        for attempt in range(config.MAX_RETRIES):
            try:
                resp = self.session.post(
                    url,
                    data=data,
                    headers={'Content-Type': 'application/x-www-form-urlencoded'},
                    timeout=10,
                )
                if resp.status_code == 403:
                    raise TokenExpiredError(f"403 from Kite: {resp.text}")
                if resp.status_code == 429:
                    print(f"[kite] 429 rate limit on {path}, sleeping 1s")
                    time.sleep(1)
                    continue
                if resp.status_code >= 400:
                    raise KiteAPIError(f"{resp.status_code} on {path}: {resp.text}")
                return resp.json().get('data')
            except TokenExpiredError:
                raise
            except KiteAPIError as e:
                last_err = e
                print(f"[kite] POST error attempt {attempt+1}: {e}")
                time.sleep(1)
            except requests.RequestException as e:
                last_err = e
                print(f"[kite] POST network error attempt {attempt+1}: {e}")
                time.sleep(2)
        raise KiteAPIError(f"POST max retries exceeded for {path}: {last_err}")

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
        today = datetime.now().date()
        if self._instrument_cache_date == today and self._instrument_cache:
            return list(self._instrument_cache.values())

        text = self._get('/instruments', raw=True)
        rows = []
        reader = csv.DictReader(io.StringIO(text))
        new_cache = {}
        new_map = {}
        for row in reader:
            try:
                token = int(row.get('instrument_token') or 0)
            except ValueError:
                continue
            exch = row.get('exchange', '')
            tsym = row.get('tradingsymbol', '')
            key = f"{exch}:{tsym}"
            new_cache[key] = row
            new_map[key] = token
            rows.append(row)
        self._instrument_cache = new_cache
        self._instrument_map = new_map
        self._instrument_cache_date = today
        return rows

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
        exchange: str,
        transaction_type: str,
        quantity: int,
        order_type: str = 'MARKET',
        product: str = 'MIS',
    ):
        try:
            data = {
                'tradingsymbol': symbol,
                'exchange': exchange,
                'transaction_type': transaction_type,
                'quantity': quantity,
                'order_type': order_type,
                'product': product,
                'validity': 'DAY',
            }
            res = self._post('/orders/regular', data=data) or {}
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
