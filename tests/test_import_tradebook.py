"""Console tradebook export -> tradebook rows."""
import sys
sys.path.insert(0, 'scripts')
import import_tradebook as it

TABLE = [
    [None, 'Client ID', 'UR0000'],
    [None, 'Tradebook for Equity from 2026-04-01 to 2026-11-12'],
    [None, ''],
    [None, 'Symbol', 'ISIN', 'Trade Date', 'Exchange', 'Segment', 'Series', 'Trade Type', 'Auction',
     'Quantity', 'Price', 'Trade ID', 'Order ID', 'Order Execution Time'],
    [None, 'TATAPOWER', 'INE245A01021', '2026-04-01', 'NSE', 'EQ', 'EQ', 'sell', False, 50.0, 381.0,
     '608591307', '1300000096883861', '2026-04-01T15:10:22'],
    [None, None, None],
]


def test_parse_console_export():
    rows = it.parse(TABLE)
    assert len(rows) == 1
    r = rows[0]
    assert (r['symbol'], r['trade_type'], r['quantity'], r['price'], r['trade_id'], r['order_id']) == \
           ('TATAPOWER', 'sell', 50.0, 381.0, '608591307', '1300000096883861')
    assert r['executed_at'] == '2026-04-01T15:10:22+05:30' and r['source'] == 'console_import'
