"""Holdings snapshot capture (Task 2).

Persists one complete book capture so the risk estimators (advisor_risk) read
stored state instead of needing a live token every time. Append-only: a changed
book is a new snapshot_id, never an overwrite.

Fail-loud, write-nothing: all rows are gathered before the single insert, so a
token/API failure raises and writes NOTHING rather than a partial book. Listed
names carry a Kite last_price; fund holdings carry units + identifier and a NULL
price (valued later by the AMFI NAV join), so an unmapped scheme stays NULL and
I-8 catches it rather than a fabricated price hiding it.
"""
import datetime as _dt
import uuid as _uuid

import database as db


def build_rows(market_data, source: str = 'live'):
    """Gather (snapshot_id, rows) WITHOUT writing. Raises on holdings-read
    failure (dead token, API error) so nothing partial is persisted."""
    holdings = market_data.kite.get_holdings()      # raises on token failure
    if holdings is None:
        raise RuntimeError('get_holdings returned None — refusing to snapshot')

    sid = _uuid.uuid4().hex
    captured = _dt.datetime.now(_dt.timezone.utc).isoformat()
    rows = []
    for h in holdings:
        tsym = h.get('tradingsymbol')
        if not tsym:
            continue
        rows.append({
            'snapshot_id': sid, 'captured_at': captured,
            'identifier': tsym, 'isin': h.get('isin'),
            'instrument_type': 'listed',
            'quantity': float(h.get('quantity') or 0),
            'average_price': float(h.get('average_price') or 0) or None,
            'last_price': float(h.get('last_price') or 0) or None,
            'source': source,
        })

    # Fund holdings: from the hand-curated, CONFIRMED map only. Units + code,
    # NULL price (AMFI join values them). Unconfirmed rows are never snapshotted.
    for m in db.get_confirmed_holding_map():
        rows.append({
            'snapshot_id': sid, 'captured_at': captured,
            'identifier': m['amfi_scheme_code'], 'isin': None,
            'instrument_type': 'fund',
            'quantity': float(m.get('units') or 0),
            'average_price': None, 'last_price': None,
            'source': f"{source}:holding_scheme_map",
        })

    if not rows:
        raise RuntimeError('no holdings captured (empty listed book and no '
                           'confirmed fund map) — refusing to write an empty '
                           'snapshot')
    return sid, rows


def capture_snapshot(market_data, source: str = 'live') -> dict:
    """Build then persist one snapshot in a single insert. Returns
    {snapshot_id, rows, listed, funds}. Raises (writes nothing) on failure."""
    sid, rows = build_rows(market_data, source)
    n = db.insert_holdings_snapshot(rows)
    listed = sum(1 for r in rows if r['instrument_type'] == 'listed')
    funds = sum(1 for r in rows if r['instrument_type'] == 'fund')
    print(f"[holdings_snapshot] wrote {n} rows (listed={listed} fund={funds}) "
          f"snapshot_id={sid}")
    return {'snapshot_id': sid, 'rows': n, 'listed': listed, 'funds': funds}
