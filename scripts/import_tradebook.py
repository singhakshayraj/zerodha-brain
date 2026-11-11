"""Import a Zerodha Console tradebook export (Equity) into `tradebook`.

    python3 scripts/import_tradebook.py data/console/tradebook-EQ-<date>.xlsx

Console -> Reports -> Tradebook -> Equity -> date range -> download (.xlsx or
.csv). Upserts on (exchange, trade_id, order_id), the same key the live
/trades sync writes, so overlaps with API rows are no-ops. Prints the date
range covered and any day in the range that still has no fills on record.
"""
import csv
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

COLS = {'Symbol': 'symbol', 'ISIN': 'isin', 'Trade Date': 'trade_date',
        'Exchange': 'exchange', 'Segment': 'segment', 'Series': 'series',
        'Trade Type': 'trade_type', 'Quantity': 'quantity', 'Price': 'price',
        'Trade ID': 'trade_id', 'Order ID': 'order_id',
        'Order Execution Time': 'executed_at'}


def read_rows(path: str) -> list:
    """Raw table rows (list of lists) from .xlsx or .csv, header included."""
    if path.endswith('.xlsx'):
        import openpyxl
        ws = openpyxl.load_workbook(path).worksheets[0]
        return [list(r) for r in ws.iter_rows(values_only=True)]
    with open(path, newline='') as f:
        return list(csv.reader(f))


def parse(table: list) -> list:
    """Pure: Console table -> tradebook rows. Header is the row containing
    'Symbol' and 'Trade ID'; leading blank columns and title rows ignored."""
    hdr_i = next(i for i, r in enumerate(table)
                 if r and 'Symbol' in [str(c).strip() for c in r if c is not None]
                 and 'Trade ID' in [str(c).strip() for c in r if c is not None])
    hdr = [str(c).strip() if c is not None else '' for c in table[hdr_i]]
    idx = {COLS[h]: i for i, h in enumerate(hdr) if h in COLS}
    out = []
    for r in table[hdr_i + 1:]:
        if not r or len(r) <= idx['trade_id'] or r[idx['symbol']] in (None, ''):
            continue
        g = lambda k: r[idx[k]] if k in idx and idx[k] < len(r) else None
        ts = str(g('executed_at') or '').replace(' ', 'T')
        out.append({
            'symbol': str(g('symbol')).strip(), 'isin': g('isin'),
            'trade_date': str(g('trade_date'))[:10], 'exchange': g('exchange') or 'NSE',
            'segment': g('segment') or 'EQ', 'series': g('series'),
            'trade_type': str(g('trade_type')).strip().lower(),
            'quantity': float(g('quantity')), 'price': float(g('price')),
            'trade_id': str(g('trade_id')).strip(), 'order_id': str(g('order_id')).strip(),
            'executed_at': (ts + '+05:30') if ts and '+' not in ts else (ts or None),
            'source': 'console_import',
        })
    return out


def main():
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    import database as db
    rows = parse(read_rows(sys.argv[1]))
    if not rows:
        sys.exit("no fills found in the file")
    n = 0
    for i in range(0, len(rows), 500):
        db.supabase.table('tradebook').upsert(
            rows[i:i + 500], on_conflict='exchange,trade_id,order_id').execute()
        n += len(rows[i:i + 500])
    dates = sorted({r['trade_date'] for r in rows})
    print(f"imported {n} fills · {len(dates)} trading days · {dates[0]} → {dates[-1]} · "
          f"{len({r['symbol'] for r in rows})} symbols "
          f"({sum(r['trade_type'] == 'buy' for r in rows)} buys / "
          f"{sum(r['trade_type'] == 'sell' for r in rows)} sells)")


if __name__ == '__main__':
    main()
