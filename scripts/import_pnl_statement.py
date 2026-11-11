"""Import a Zerodha Console P&L statement (Equity) and reconcile it.

    python3 scripts/import_pnl_statement.py <file.xlsx> [--account primary]

Console -> Reports -> P&L -> Equity -> date range -> download. Stores the
statement in `pnl_statements` and checks it against our `tradebook`: for
every symbol, Zerodha's Buy Value / Sell Value over the period must equal the
sum of our fills in the same window (within TOLERANCE rupees). A mismatch
means a missing or extra fill on our side. Zerodha's realized / unrealized /
charges are kept as the authoritative figures for the period.
"""
import os
import re
import sys
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

TOLERANCE = 1.0
SYM_COLS = {'Symbol': 'symbol', 'Quantity': 'qty', 'Buy Value': 'buy_value',
            'Sell Value': 'sell_value', 'Realized P&L': 'realized',
            'Previous Closing Price': 'prev_close', 'Open Quantity': 'open_qty',
            'Open Value': 'open_value', 'Unrealized P&L': 'unrealized'}


def _cells(r):
    return [c for c in r if c is not None and str(c).strip() != '']


def parse(sheets: dict) -> dict:
    """Pure: {sheet_name: [rows]} -> statement dict."""
    eq = sheets.get('Equity') or next(iter(sheets.values()))
    period, summary, symbols, hdr = None, {}, [], None
    for r in eq:
        c = _cells(r)
        if not c:
            continue
        m = re.search(r'from (\d{4}-\d{2}-\d{2}) to (\d{4}-\d{2}-\d{2})', str(c[0]))
        if m:
            period = m.groups()
            continue
        if hdr is None and len(c) == 2 and str(c[0]) in (
                'Charges', 'Other Credit & Debit', 'Realized P&L', 'Unrealized P&L') \
                and isinstance(c[1], (int, float)):
            summary[str(c[0])] = float(c[1])
            continue
        if 'Symbol' in c and 'Buy Value' in c:
            hdr = {str(h): i for i, h in enumerate(r) if h is not None}
            continue
        if hdr and r[hdr['Symbol']]:
            symbols.append({k: (r[hdr[h]] if k == 'symbol' else float(r[hdr[h]] or 0))
                            for h, k in SYM_COLS.items() if h in hdr})
    other = []
    od = sheets.get('Other Debits and Credits') or []
    oh = None
    for r in od:
        c = _cells(r)
        if 'Particulars' in c:
            oh = {str(h): i for i, h in enumerate(r) if h is not None}
            continue
        if oh and c and r[oh['Particulars']]:
            other.append({'particulars': r[oh['Particulars']], 'date': str(r[oh['Posting Date']])[:10],
                          'debit': float(r[oh['Debit']] or 0), 'credit': float(r[oh['Credit']] or 0)})
    if not period:
        raise ValueError("no 'from YYYY-MM-DD to YYYY-MM-DD' line found")
    return {'period_from': period[0], 'period_to': period[1], 'summary': summary,
            'symbols': symbols, 'other_debits': other}


def check(statement: dict, fills: list, settled: dict = None, lag_from: str = None) -> dict:
    """Pure. Two checks, both exact:
    - sells: Zerodha's Sell Value per symbol == our sell fills in the period
      (its Buy Value is the cost of the shares sold, not the period's buys,
      so buys are not comparable line by line);
    - open qty (only when `settled` holdings are given, i.e. the statement
      ends today): Zerodha's Open Quantity == settled holdings now.
    Console posts a day's trades the next day: symbols with fills on or after
    `lag_from` are reported as pending, not as mismatches."""
    lo, hi = statement['period_from'], statement['period_to']
    sells = defaultdict(float)
    pending = sorted({f['symbol'] for f in fills
                      if lag_from and lag_from <= str(f['trade_date']) <= hi})
    for f in fills:
        if f['trade_type'] == 'sell' and lo <= str(f['trade_date']) <= hi:
            sells[f['symbol']] += float(f['quantity']) * float(f['price'])
    for sym in pending:
        sells.pop(sym, None)
    first = min((str(f['trade_date']) for f in fills), default=None)
    sell_bad, qty_bad = [], []
    for s in statement['symbols']:
        if s['symbol'] in pending:
            continue
        ours = round(sells.pop(s['symbol'], 0.0), 2)
        if abs(ours - s['sell_value']) > TOLERANCE:
            sell_bad.append({'symbol': s['symbol'], 'zerodha': s['sell_value'], 'ours': ours})
        if settled is not None:
            have = float(settled.get(s['symbol'], 0))
            if abs(have - s['open_qty']) > 1e-6:
                qty_bad.append({'symbol': s['symbol'], 'zerodha': s['open_qty'], 'ours': have})
    for sym, v in sells.items():
        if v:
            sell_bad.append({'symbol': sym, 'zerodha': 0.0, 'ours': round(v, 2)})
    if settled is not None:
        listed = {s['symbol'] for s in statement['symbols']}
        qty_bad += [{'symbol': k, 'zerodha': 0.0, 'ours': float(v)}
                    for k, v in settled.items() if v and k not in listed and k not in pending]
    sold = [s for s in statement['symbols'] if s['sell_value'] and s['symbol'] not in pending]
    return {'sell_symbols': len(sold) + sum(1 for m in sell_bad if m['zerodha'] == 0),
            'sell_matched': len(sold) - sum(1 for m in sell_bad if m['zerodha']),
            'sell_mismatches': sell_bad,
            'open_checked': settled is not None, 'open_mismatches': qty_bad,
            'pending': pending, 'lag_from': lag_from,
            'fills_on_record_from': first, 'covers_period': bool(first and first <= lo)}


def main():
    args = sys.argv[1:]
    if not args:
        sys.exit(__doc__)
    account = 'primary'
    if '--account' in args:
        account = args[args.index('--account') + 1]
        args = [a for a in args if a not in ('--account', account)]
    import openpyxl
    import database as db
    import reconcile
    wb = openpyxl.load_workbook(args[0])
    st = parse({ws.title: [list(r) for r in ws.iter_rows(values_only=True)] for ws in wb.worksheets})
    fills = reconcile._paged(lambda: db.supabase.table('tradebook')
                             .select('symbol,trade_date,trade_type,quantity,price')
                             .eq('account', account).order('trade_date'))
    settled = None
    from datetime import date, timedelta
    if st['period_to'] >= (date.today() - timedelta(days=3)).isoformat():
        try:                                 # statement ends ~today: check open qty too
            from kite_client import KiteClient
            import account_preview
            key = 'enc_token' if account == 'primary' else account_preview.token_key(account)
            settled = {h['tradingsymbol']: h.get('settled_quantity', h.get('quantity'))
                       for h in KiteClient(db.get_config(key)).get_holdings() or []}
        except Exception as e:
            print(f"open-qty check skipped (no live token): {e}")
    # Console posts trades the next day: anything traded in the last 2 days
    # of a statement that ends ~today is pending, not wrong.
    lag_from = ((date.today() - timedelta(days=2)).isoformat()
                if st['period_to'] >= (date.today() - timedelta(days=1)).isoformat() else None)
    res = check(st, fills, settled, lag_from)
    db.supabase.table('pnl_statements').upsert({
        'account': account, 'period_from': st['period_from'], 'period_to': st['period_to'],
        'summary': st['summary'], 'symbols': st['symbols'], 'other_debits': st['other_debits'],
        'check_result': res}, on_conflict='account,period_from,period_to').execute()
    s = st['summary']
    print(f"{account} {st['period_from']} → {st['period_to']}: realized ₹{s.get('Realized P&L', 0):,.0f} · "
          f"unrealized ₹{s.get('Unrealized P&L', 0):,.0f} · charges ₹{s.get('Charges', 0):,.0f} · "
          f"other ₹{s.get('Other Credit & Debit', 0):,.0f}")
    gap = '' if res['covers_period'] else \
        f" (our fills start {res['fills_on_record_from']}; earlier sells are not on record)"
    print(f"sells: {res['sell_matched']}/{res['sell_symbols']} symbols match Zerodha{gap}")
    if res['pending']:
        print(f"pending (traded since {res['lag_from']}, Console not updated yet): {', '.join(res['pending'])}")
    for m in res['sell_mismatches'][:20]:
        print(f"  ✗ sell {m['symbol']:12} Zerodha ₹{m['zerodha']:,.2f} vs ours ₹{m['ours']:,.2f}")
    if res['open_checked']:
        print(f"open qty: {'all match' if not res['open_mismatches'] else str(len(res['open_mismatches'])) + ' differ'}")
        for m in res['open_mismatches'][:20]:
            print(f"  ✗ open {m['symbol']:12} Zerodha {m['zerodha']:g} vs Kite settled {m['ours']:g}")

if __name__ == '__main__':
    main()
