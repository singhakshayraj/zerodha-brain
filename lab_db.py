"""Strategy-lab persistence [P-70]: lab_strategies / lab_trades / lab_daily /
lab_refusals (scripts/lab_tables_2026-11-18.sql). Trades are written OPEN at
entry and closed in place, so a brain restart can reload today's positions."""
import database as db

_OPEN_FIELDS = ('strategy_id', 'symbol', 'side', 'qty', 'entry_price', 'entry_ts', 'stop',
                'target', 'slip_bps', 'risk_inr', 'entry_charges', 'entry_slip_inr', 'entry_ref')
_CLOSE_FIELDS = ('exit_ts', 'exit_price', 'exit_reason', 'gross_inr', 'charges_inr',
                 'slippage_inr', 'net_inr', 'gross_r', 'cost_r', 'net_r')


def register(rows: list) -> None:
    have = {r['id']: r for r in (db.supabase.table('lab_strategies').select('id,spec_hash')
                                 .execute().data or [])}
    changed = [r['id'] for r in rows if r['id'] in have and have[r['id']]['spec_hash'] != r['spec_hash']]
    if changed:
        raise ValueError(f'lab variants changed after registration (bump to _v2): {changed}')
    new = [r for r in rows if r['id'] not in have]
    if new:
        db.supabase.table('lab_strategies').insert(new).execute()


def insert_open(pos, trade_date: str) -> int:
    row = {k: getattr(pos, k) for k in _OPEN_FIELDS}
    row.update(trade_date=trade_date, status='OPEN')
    return db.supabase.table('lab_trades').insert(row).execute().data[0]['id']


def close_trade(db_id: int, row: dict) -> None:
    vals = {k: row[k] for k in _CLOSE_FIELDS}
    vals['status'] = 'CLOSED'
    db.supabase.table('lab_trades').update(vals).eq('id', db_id).execute()


def load_open(trade_date: str) -> list:
    return (db.supabase.table('lab_trades').select('*').eq('trade_date', trade_date)
            .eq('status', 'OPEN').execute().data or [])


def closed_on(trade_date: str) -> list:
    out, start = [], 0
    while True:
        page = (db.supabase.table('lab_trades').select('*').eq('trade_date', trade_date)
                .eq('status', 'CLOSED').order('id').range(start, start + 999).execute().data or [])
        out += page
        if len(page) < 1000:
            return out
        start += 1000


def bump_refusals(trade_date: str, counts: dict) -> None:
    rows = [{'trade_date': trade_date, 'strategy_id': sid, 'reason': reason, 'count': n}
            for (sid, reason), n in counts.items()]
    if rows:
        db.supabase.table('lab_refusals').upsert(
            rows, on_conflict='trade_date,strategy_id,reason').execute()


def write_daily(trade_date: str, rows: list) -> None:
    if rows:
        db.supabase.table('lab_daily').upsert(
            [{'trade_date': trade_date, **r} for r in rows],
            on_conflict='trade_date,strategy_id').execute()


def daily_history(strategy_id: str) -> list:
    return (db.supabase.table('lab_daily').select('*').eq('strategy_id', strategy_id)
            .order('trade_date').execute().data or [])


def statuses() -> dict:
    return {r['id']: r.get('status') or 'running' for r in (db.supabase.table('lab_strategies')
                                           .select('id,status').execute().data or [])}


def set_status(strategy_id: str, status: str) -> None:
    db.supabase.table('lab_strategies').update({'status': status}).eq('id', strategy_id).execute()
