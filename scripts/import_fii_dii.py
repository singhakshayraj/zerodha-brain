"""Merge the operator's daily participant files into macro_daily (P-52).

    python3 scripts/import_fii_dii.py /Users/.../FIIDII

Reads from that folder (each optional; missing files are skipped):
  fii_dii_history.csv   cash-market FII/DII buy/sell/net, Rs crore
                        -> data.fii, data.dii, data.fii_net_cr, data.dii_net_cr
  fii_fut_history.csv   index-futures positioning, contracts
                        -> data.fut = {fii_long, fii_short, fii_long_share_pct,
                           fii_net, dii_net, pro_net, client_net, dup_of_prev}
  nifty_valuation.csv   Nifty PE/PB/yield + percentiles
                        -> data.valuation

Other keys on a date's row are kept. Dates with no macro row yet get one
with just these fields; the morning macro run fills the rest. A futures row
whose six values exactly repeat the previous session is flagged
dup_of_prev=True (scrape artifact), never silently counted twice.
"""
import csv
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

FUT_KEYS = ('fii_long', 'fii_short', 'fii_long_share_pct', 'fii_net', 'dii_net',
            'pro_net', 'client_net')
VAL_KEYS = ('pe', 'pe_percentile', 'pe_5y_avg', 'pe_10y_avg', 'pb', 'pb_percentile',
            'div_yield', 'div_yield_percentile')


def _read(path: str) -> list:
    if not os.path.exists(path):
        return []
    with open(path, newline='') as f:
        return list(csv.DictReader(f))


def _f(r, k):
    try:
        return round(float(r[k]), 2)
    except (KeyError, ValueError, TypeError):
        return None


def parse_cash(rows: list) -> dict:
    out = {}
    for r in rows:
        v = {k: _f(r, k) for k in ('fii_buy', 'fii_sell', 'fii_net', 'dii_buy', 'dii_sell', 'dii_net')}
        if v['fii_net'] is None or v['dii_net'] is None:
            continue
        out[r['date']] = {'fii': {'buy': v['fii_buy'], 'sell': v['fii_sell'], 'net': v['fii_net']},
                          'dii': {'buy': v['dii_buy'], 'sell': v['dii_sell'], 'net': v['dii_net']},
                          'fii_net_cr': v['fii_net'], 'dii_net_cr': v['dii_net']}
    return out


def parse_futures(rows: list) -> dict:
    """Pure; flags exact repeats of the previous session's six numbers."""
    out, prev = {}, None
    for r in sorted(rows, key=lambda r: r['date']):
        v = {k: _f(r, k) for k in FUT_KEYS}
        if v['fii_net'] is None:
            continue
        sig = tuple(v[k] for k in ('fii_long', 'fii_short', 'fii_net', 'dii_net', 'pro_net', 'client_net'))
        v['dup_of_prev'] = sig == prev
        prev = sig
        out[r['date']] = {'fut': v}
    return out


def parse_valuation(rows: list) -> dict:
    out = {}
    for r in rows:
        v = {k: _f(r, k) for k in VAL_KEYS}
        if v['pe'] is not None:
            out[r['date']] = {'valuation': v}
    return out


def merge(existing: dict, *parts: dict) -> dict:
    """Pure: parts merged into an existing data dict (other keys kept)."""
    out = dict(existing or {})
    for p in parts:
        out.update(p)
    return out


def main():
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    folder = sys.argv[1]
    import database as db
    cash = parse_cash(_read(os.path.join(folder, 'fii_dii_history.csv')))
    fut = parse_futures(_read(os.path.join(folder, 'fii_fut_history.csv')))
    val = parse_valuation(_read(os.path.join(folder, 'nifty_valuation.csv')))
    dates = sorted(set(cash) | set(fut) | set(val))
    if not dates:
        sys.exit("nothing to import")
    have = {r['date']: r['data'] for r in (db.supabase.table('macro_daily').select('date,data')
                                           .gte('date', dates[0]).lte('date', dates[-1])
                                           .execute().data or [])}
    rows = [{'date': d, 'data': merge(have.get(d), cash.get(d, {}), fut.get(d, {}), val.get(d, {}))}
            for d in dates]
    for i in range(0, len(rows), 200):
        db.supabase.table('macro_daily').upsert(rows[i:i + 200], on_conflict='date').execute()
    dups = [d for d, v in fut.items() if v['fut']['dup_of_prev']]
    lc = max(cash) if cash else None
    lf = max(fut) if fut else None
    lv = max(val) if val else None
    cash_note = f", FII net ₹{cash[lc]['fii_net_cr']:,.0f} cr" if lc else ''
    fut_note = f", FII long share {fut[lf]['fut']['fii_long_share_pct']}%" if lf else ''
    print(f"[fii_dii] {len(rows)} dates merged into macro_daily — cash {len(cash)} "
          f"(latest {lc}{cash_note}) · futures {len(fut)} (latest {lf}{fut_note}) · "
          f"valuation {len(val)} (latest {lv})"
          + (f" · ⚠ futures rows repeating the prior session: {', '.join(dups)}" if dups else ''))

if __name__ == '__main__':
    main()
