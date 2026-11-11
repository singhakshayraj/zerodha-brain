"""Merge the operator's FII/DII history CSV into macro_daily (P-52).

    python3 scripts/import_fii_dii.py /Users/.../FIIDII/fii_dii_history.csv

CSV columns: date,fii_buy,fii_sell,fii_net,dii_buy,dii_sell,dii_net (Rs crore,
NSE cash market). Each date's values land at macro_daily.data.fii / .dii and
data.fii_net_cr; other keys on the row are kept. Dates with no macro row yet
(e.g. the file is ahead of the morning bar sync) get a row with only the
flows; the next macro run fills the rest. Idempotent.
"""
import csv
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def parse(path: str) -> dict:
    """{date: {'fii': {...}, 'dii': {...}, 'fii_net_cr': x, 'dii_net_cr': y}}"""
    out = {}
    with open(path, newline='') as f:
        for r in csv.DictReader(f):
            try:
                g = lambda k: round(float(r[k]), 2)
                out[r['date']] = {
                    'fii': {'buy': g('fii_buy'), 'sell': g('fii_sell'), 'net': g('fii_net')},
                    'dii': {'buy': g('dii_buy'), 'sell': g('dii_sell'), 'net': g('dii_net')},
                    'fii_net_cr': g('fii_net'), 'dii_net_cr': g('dii_net'),
                }
            except (KeyError, ValueError):
                continue
    return out


def merge(existing: dict, flows: dict) -> dict:
    """Pure: flows merged into an existing data dict (other keys kept)."""
    return {**(existing or {}), **flows}


def main():
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    import database as db
    flows = parse(sys.argv[1])
    if not flows:
        sys.exit("no rows parsed")
    dates = sorted(flows)
    have = {r['date']: r['data'] for r in (db.supabase.table('macro_daily').select('date,data')
                                           .gte('date', dates[0]).lte('date', dates[-1])
                                           .execute().data or [])}
    rows = [{'date': d, 'data': merge(have.get(d), flows[d])} for d in dates]
    for i in range(0, len(rows), 200):
        db.supabase.table('macro_daily').upsert(rows[i:i + 200], on_conflict='date').execute()
    new = [d for d in dates if d not in have]
    print(f"[fii_dii] {len(rows)} dates {dates[0]} → {dates[-1]} merged into macro_daily "
          f"({len(new)} had no macro row yet); latest FII net ₹{flows[dates[-1]]['fii_net_cr']:,.0f} cr, "
          f"DII net ₹{flows[dates[-1]]['dii_net_cr']:,.0f} cr")


if __name__ == '__main__':
    main()
