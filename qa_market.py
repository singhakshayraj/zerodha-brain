"""QA-mode fake Kite client — synthetic market for off-hours rehearsals.

Drop-in for KiteClient when config.QA_MODE is on. Generates a random-walk
market per symbol (seeded per process, per-symbol trend + volatility) so the
real brain, real signal engine, real risk manager and real paper broker run
the exact production code path against the sim Supabase at any hour.

Implements only the surface the brain actually touches:
  get_holdings, get_instruments, get_ltp, get_positions,
  _get('/instruments/historical/<token>/<interval>', ...)
"""

import os
import random
import time
from datetime import datetime, timedelta

import pytz

import config
from kite_client import KiteAPIError, TokenExpiredError

IST = pytz.timezone('Asia/Kolkata')

# interval string -> minutes per candle
_INTERVAL_MINUTES = {'5minute': 5, '15minute': 15, '60minute': 60}

# candles to generate per interval — enough for every indicator (needs 35+)
_CANDLE_COUNT = {'5minute': 200, '15minute': 150, '60minute': 120}


class FakeKiteClient:
    def __init__(self):
        # Reverse map: token -> 'NSE:SYMBOL'
        self._token_to_symbol = {
            tok: sym for sym, tok in config.NIFTY50_INSTRUMENT_TOKENS.items()
        }
        self._state = {}  # symbol -> {'price','trend','vol','base'}
        self._seed = int(time.time())
        # Chaos fault injection (REQ-083). QA_FAULT="kind@N": after N market
        # data fetches, start injecting the fault. Kinds:
        #   token_expiry@N        raise TokenExpiredError once N reached
        #   network_drop@N        raise KiteAPIError for a burst then recover
        #   network_drop_hard@N   raise KiteAPIError permanently from N on
        self._fault_kind, self._fault_after = self._parse_fault()
        self._fetches = 0
        print(
            f"[QA] FakeKiteClient active — synthetic market, seed={self._seed}"
            + (f", fault={self._fault_kind}@{self._fault_after}" if self._fault_kind else "")
        )

    @staticmethod
    def _parse_fault():
        spec = os.getenv('QA_FAULT', '').strip()
        if not spec or '@' not in spec:
            return None, 0
        kind, _, n = spec.partition('@')
        try:
            return kind, int(n)
        except ValueError:
            return None, 0

    def _maybe_fault(self):
        if not self._fault_kind:
            return
        self._fetches += 1
        if self._fetches < self._fault_after:
            return
        n = self._fetches - self._fault_after
        if self._fault_kind == 'token_expiry':
            raise TokenExpiredError("[QA_FAULT] injected token expiry")
        if self._fault_kind == 'network_drop':
            # transient: fail a 3-fetch burst, then recover
            if n < 3:
                raise KiteAPIError("[QA_FAULT] injected transient network drop")
        if self._fault_kind == 'network_drop_hard':
            raise KiteAPIError("[QA_FAULT] injected sustained network drop")

    # ── price engine ─────────────────────────────────────────────────────────

    def _sym_state(self, symbol: str) -> dict:
        if symbol not in self._state:
            rng = random.Random(f"{self._seed}:{symbol}")
            base = rng.uniform(80, 3000)
            self._state[symbol] = {
                'base': base,
                'price': base,
                # per-symbol personality: some strong trends, some chop —
                # strong enough that the real signal engine occasionally
                # fires BUY/SELL, so QA exercises the fill path too
                'trend': rng.choice([-3, -2, -1, 0, 0, 1, 2, 3]) * 0.0012,
                'vol': rng.uniform(0.002, 0.006),
                'rng': random.Random(f"{self._seed}:{symbol}:walk"),
            }
        return self._state[symbol]

    def _step(self, s: dict) -> float:
        rng = s['rng']
        s['price'] *= 1 + s['trend'] + rng.gauss(0, s['vol'])
        # keep within a sane band of base so nothing collapses to 0
        s['price'] = max(s['base'] * 0.5, min(s['base'] * 2.0, s['price']))
        return s['price']

    def _candles(self, symbol: str, interval: str) -> list:
        """Walk history forward so candles are internally consistent and the
        final close equals the symbol's current live price."""
        s = self._sym_state(symbol)
        n = _CANDLE_COUNT.get(interval, 150)
        minutes = _INTERVAL_MINUTES.get(interval, 15)
        rng = random.Random(f"{self._seed}:{symbol}:{interval}")

        # Rebuild a walk that ends at the current price
        closes = [s['price']]
        for _ in range(n - 1):
            closes.append(closes[-1] / (1 + s['trend'] + rng.gauss(0, s['vol'])))
        closes.reverse()

        now = datetime.now(IST)
        out = []
        for i, close in enumerate(closes):
            ts = now - timedelta(minutes=minutes * (n - 1 - i))
            spread = abs(rng.gauss(0, s['vol'])) * close
            o = close * (1 + rng.gauss(0, s['vol'] / 2))
            out.append([
                ts.strftime('%Y-%m-%dT%H:%M:%S+0530'),
                round(o, 2),
                round(max(o, close) + spread, 2),
                round(min(o, close) - spread, 2),
                round(close, 2),
                int(abs(rng.gauss(50_000, 20_000))) + 1_000,
            ])
        return out

    # ── KiteClient surface ───────────────────────────────────────────────────

    def get_holdings(self) -> list:
        # Two fake CNC holdings so the HOLDINGS/BOTH paths get exercised too
        out = []
        for sym in list(config.NIFTY50_INSTRUMENT_TOKENS)[:2]:
            tsym = sym.split(':', 1)[1]
            s = self._sym_state(sym)
            out.append({
                'tradingsymbol': tsym,
                'exchange': 'NSE',
                'quantity': 10,
                'average_price': round(s['base'], 2),
                'last_price': round(s['price'], 2),
                'instrument_token': config.NIFTY50_INSTRUMENT_TOKENS[sym],
            })
        return out

    def get_instruments(self) -> list:
        return []

    def get_positions(self) -> dict:
        return {'day': [], 'net': []}

    def get_profile(self) -> dict:
        return {'user_id': 'QA0000', 'user_name': 'QA Harness'}

    def get_ltp(self, symbols: list) -> dict:
        self._maybe_fault()
        out = {}
        for instrument in symbols:
            sym = instrument if instrument.startswith('NSE:') else f'NSE:{instrument}'
            s = self._sym_state(sym)
            out[instrument] = {'last_price': round(self._step(s), 2)}
        return out

    def get_quote(self, symbols: list) -> dict:
        return self.get_ltp(symbols)

    def _get(self, path: str, params: dict = None, raw: bool = False):
        if '/instruments/historical/' in path:
            self._maybe_fault()
            parts = path.strip('/').split('/')
            token = int(parts[2])
            interval = parts[3]
            symbol = self._token_to_symbol.get(token)
            if not symbol:
                return {'candles': []}
            # advance the live price a step per poll so cycles see movement
            self._step(self._sym_state(symbol))
            return {'candles': self._candles(symbol, interval)}
        return {}
