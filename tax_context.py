"""[P-68] Reads that feed tax_actions.plan(): FIFO lots, this FY's realised P&L,
the stored SELL-call cost curve, and 20-day ADV. See tax_actions for contracts."""
import json
from datetime import date, timedelta
from statistics import mean

import database as db
import call_outcomes
import tax_actions
from costs import tax_lots
from universe_calls import _paged

CURVE_TDS = (5, 10, 20, 40, 60)


def cost_curve(stored_rows_by_call: dict, calls: list) -> dict:
    """{'<td>': {n, mean_ret_pct}} over SELL-grade advice calls' stored outcome rows."""
    rets = {td: [] for td in CURVE_TDS}
    for c in calls:
        if c['source'] != 'advice' or c['attrs'].get('verdict') not in tax_actions.SELL_GRADES:
            continue
        for r in stored_rows_by_call.get((c['source'], c['account'], c['run_date'], c['symbol'])) or []:
            if r.get('td') in rets and r.get('ret_pct') is not None:
                rets[r['td']].append(float(r['ret_pct']))
    return {str(td): {'n': len(v), 'mean_ret_pct': round(mean(v), 2)} for td, v in rets.items() if v}


def _fy(account: str, today: str) -> dict:
    y = int(today[:4]) - (today[5:7] < '04')
    rows = db.supabase.table('pnl_statements').select('summary').eq('account', account) \
        .eq('period_from', f'{y}-04-01').order('period_to', desc=True).limit(1).execute().data or []
    summ = rows[0].get('summary') if rows else None
    if isinstance(summ, str):
        summ = json.loads(summ)
    if not summ or summ.get('Realized P&L') is None:
        return {'ytd_net_realised_inr': 0.0, 'ltcg_used_fy': 0.0, 'fy_known': False}
    # ltcg_used_fy stays 0.0: the statement has no STCG/LTCG split (ledgered ruling)
    return {'ytd_net_realised_inr': float(summ['Realized P&L']), 'ltcg_used_fy': 0.0, 'fy_known': True}


def _adv(symbols: list, today: str) -> dict:
    if not symbols:
        return {}
    since = (date.fromisoformat(today) - timedelta(days=40)).isoformat()
    rows = _paged(lambda: db.supabase.table('candles').select('symbol,trade_date,close,volume')
                  .eq('interval', 'day').in_('symbol', symbols).gte('trade_date', since)
                  .order('trade_date').order('symbol'))
    by = {}
    for r in rows:
        if r.get('close') is not None and r.get('volume') is not None:
            by.setdefault(r['symbol'], []).append(float(r['close']) * float(r['volume']))
    return {s: mean(v[-20:]) for s, v in by.items()}


def context_for(account: str, today: str, symbols: list) -> dict:
    trades = _paged(lambda: db.supabase.table('tradebook').select('*').eq('account', account)
                    .order('executed_at').order('trade_id')) if symbols else []
    raw = db.get_config('tax_cost_curve')
    curve = json.loads(raw) if raw else {}
    if not curve:    # fresh deploy/reset: derive from stored outcomes, as scripts/tax_replay.py does
        curve = cost_curve(call_outcomes._stored(call_outcomes.BARS_START),
                           call_outcomes.load_calls(call_outcomes.BARS_START))
    return {'lots_by_symbol': {s: tax_lots(trades, s, account) for s in symbols},
            'fy': _fy(account, today),
            'curve': curve,
            'adv_by_symbol': _adv(symbols, today)}
