"""Console P&L statement parse + fills reconciliation."""
import sys
sys.path.insert(0, 'scripts')
import import_pnl_statement as ps

SHEETS = {'Equity': [
    [None, 'P&L Statement for Equity from 2026-04-01 to 2026-11-12'],
    [None, 'Charges', 602.6], [None, 'Realized P&L', -47479.2], [None, 'Unrealized P&L', -38314.9],
    [None, 'Symbol', 'ISIN', 'Quantity', 'Buy Value', 'Sell Value', 'Realized P&L', 'Realized P&L Pct.',
     'Previous Closing Price', 'Open Quantity', 'Open Quantity Type', 'Open Value', 'Unrealized P&L', 'Unrealized P&L Pct.'],
    [None, 'RVNL', 'X', 97.0, 41507.87, 18507.6, -23000.27, -55.4, 0.0, 0.0, '', 0.0, 0.0, 0.0],
    [None, 'ITC', 'Y', 35.0, 12719.99, 10757.15, -1962.8, -15.4, 284.5, 80.0, '', 30743.3, -7983.3, -25.9],
]}


def test_parse_and_check():
    st = ps.parse(SHEETS)
    assert (st['period_from'], st['period_to']) == ('2026-04-01', '2026-11-12')
    assert st['summary']['Realized P&L'] == -47479.2 and len(st['symbols']) == 2
    fills = [{'symbol': 'RVNL', 'trade_date': '2026-05-01', 'trade_type': 'buy', 'quantity': 97, 'price': 427.916},
             {'symbol': 'RVNL', 'trade_date': '2026-11-02', 'trade_type': 'sell', 'quantity': 97, 'price': 190.8},
             {'symbol': 'ITC', 'trade_date': '2026-05-01', 'trade_type': 'sell', 'quantity': 35, 'price': 300.0},
             {'symbol': 'NEW', 'trade_date': '2026-06-01', 'trade_type': 'buy', 'quantity': 1, 'price': 10},
             {'symbol': 'RVNL', 'trade_date': '2026-03-01', 'trade_type': 'buy', 'quantity': 5, 'price': 100}]
    r = ps.check(st, fills, settled={'ITC': 80, 'RVNL': 0, 'EXTRA': 3})
    assert r['sell_matched'] == 1 and r['covers_period'] is True
    assert [m['symbol'] for m in r['sell_mismatches']] == ['ITC']     # ITC sell recorded at a different value
    assert [m['symbol'] for m in r['open_mismatches']] == ['EXTRA']   # held but not on the statement


def test_recent_trades_are_pending_not_mismatched():
    st = ps.parse(SHEETS)
    fills = [{'symbol': 'RVNL', 'trade_date': '2026-11-02', 'trade_type': 'sell', 'quantity': 97, 'price': 190.8},
             {'symbol': 'ITC', 'trade_date': '2026-05-01', 'trade_type': 'sell', 'quantity': 35, 'price': 307.347},
             {'symbol': 'ITC', 'trade_date': '2026-11-11', 'trade_type': 'sell', 'quantity': 80, 'price': 280},
             {'symbol': 'RVNL', 'trade_date': '2026-03-01', 'trade_type': 'buy', 'quantity': 5, 'price': 100}]
    r = ps.check(st, fills, settled={'RVNL': 0}, lag_from='2026-11-10')
    assert r['pending'] == ['ITC'] and r['sell_mismatches'] == [] and r['open_mismatches'] == []
    assert r['sell_matched'] == 1
