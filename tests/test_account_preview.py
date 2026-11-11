"""Second-account preview: advises another account's holdings into one
app_config blob, never into portfolio_advice."""
import json
from unittest.mock import patch

import account_preview as ap


def _bars(n=300, start=100.0, step=0.3):
    return [{'timestamp': f'2025-{1 + i // 28 % 12:02d}-{1 + i % 28:02d}T00:00:00+0530',
             'open': start + i * step, 'high': start + i * step + 1,
             'low': start + i * step - 1, 'close': start + i * step, 'volume': 1000}
            for i in range(n)]


class FakeKite:
    def get_holdings(self):
        return [{'tradingsymbol': 'AAA', 'exchange': 'NSE', 'instrument_token': 1,
                 'quantity': 10, 'average_price': 100.0, 'last_price': 180.0},
                {'tradingsymbol': 'BBB', 'exchange': 'NSE', 'instrument_token': 2,
                 'quantity': 0, 'average_price': 50.0, 'last_price': 40.0}]


class FakeMD:
    def __init__(self):
        self.kite = FakeKite()
        self._instrument_cache = {}

    def get_candles(self, key, interval, days):
        return _bars()


def test_preview_advises_holdings_and_writes_only_its_blob():
    written = {}
    with patch.object(ap.db, 'write_config', side_effect=lambda k, v: written.__setitem__(k, v)), \
         patch.object(ap.db, 'write_official_portfolio_advice', create=True) as official, \
         patch.object(ap.fundamentals, 'load_store', return_value={'by_symbol': {}}), \
         patch.object(ap, 'news_sentiment', return_value=None):
        n = ap.run_preview('natwar', FakeMD())
    assert n == 1                                   # zero-qty holding skipped
    assert list(written) == ['advisor_natwar_latest']
    official.assert_not_called()
    payload = json.loads(written['advisor_natwar_latest'])
    assert payload['account'] == 'natwar' and payload['label'] == 'Natwar Raj Singh'
    row = payload['rows'][0]
    assert row['symbol'] == 'AAA' and row['verdict'] in ('HOLD', 'TRIM', 'SELL', 'SELL_ON_BOUNCE')
    assert row['indicators']['thesis']['action'] in ('HOLD', 'ADD', 'TRIM', 'EXIT')


def test_keys():
    assert ap.token_key('natwar') == 'enc_token_natwar'
    assert ap.result_key('natwar') == 'advisor_natwar_latest'
