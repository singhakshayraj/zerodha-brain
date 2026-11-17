"""[P-68] Replay the tax-aware wait rule over past official SELL-grade calls.
Read-only. Lots as of each call date; FY context + cost curve as of today.

    python3 scripts/tax_replay.py
"""
import json
import os
import sys
from datetime import date

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
SINCE = '2026-08-25'


def summarise(decisions: list) -> dict:
    waited = [d for d in decisions if d['action'].startswith('sell after')]
    saved = round(sum(d['tax_saved_inr'] for d in waited), 2)
    drift = round(sum(d['drift_paid_inr'] for d in waited), 2)
    net = round(saved - drift, 2)
    val = sum(d['sell_value_inr'] for d in waited)
    return {'calls': len(decisions), 'told_to_wait': len(waited), 'tax_saved_inr': saved,
            'drift_paid_inr': drift, 'net_inr': net,
            'net_pct': round(net / val * 100, 2) if val else 0.0}


def main():
    import call_outcomes
    import database as db
    import tax_actions
    import tax_context
    from costs import tax_lots
    from universe_calls import _paged

    grades = list(tax_actions.SELL_GRADES)
    adv = _paged(lambda: db.supabase.table('portfolio_advice')
                 .select('symbol,run_date,verdict,quantity,avg_price,last_price,rotation_sell_qty')
                 .eq('is_official', True).gte('run_date', SINCE).in_('verdict', grades)
                 .order('run_date').order('symbol'))
    trades = _paged(lambda: db.supabase.table('tradebook').select('*').eq('account', 'primary')
                    .order('executed_at').order('trade_id'))
    today = date.today().isoformat()
    ctx = tax_context.context_for('primary', today, [])
    paths = call_outcomes._stored(SINCE)
    if not ctx['curve']:    # stored curve not written yet: derive today's from the same rows, in memory
        ctx['curve'] = tax_context.cost_curve(paths, call_outcomes.load_calls(SINCE))
    out, pending, table = [], 0, []
    for r in adv:
        rd = str(r['run_date'])[:10]
        row = {'symbol': r['symbol'], 'verdict': r['verdict'], 'quantity': r['quantity'],
               'average_price': r['avg_price'], 'last_price': r['last_price'],
               'rotation_sell_qty': r.get('rotation_sell_qty')}
        if not r.get('quantity') or not r.get('last_price'):
            continue
        lots = tax_lots([t for t in trades if str(t['trade_date'])[:10] <= rd], r['symbol'], 'primary')
        p = tax_actions.plan(row, lots, ctx['fy'], ctx['curve'], rd, None, tax_actions.nse_trading_days)
        if not p:
            continue
        d = {'symbol': r['symbol'], 'run_date': rd, 'action': p['action'], 'wait_days': p['wait_days'],
             'tax_saved_inr': 0, 'drift_paid_inr': 0, 'sell_value_inr': 0}
        if p['action'].startswith('sell after'):
            ld = p['action'].split()[-1]
            k = ('advice', 'primary', rd, r['symbol'])
            rows = sorted(paths.get(k) or [], key=lambda x: str(x['date']))
            after = [x for x in rows if str(x['date'])[:10] >= ld] or rows[-1:]
            if not after:                      # no path row at all yet
                pending += 1
                continue
            sq = tax_actions._sell_qty(row)
            d.update(tax_saved_inr=p['saving_inr'], sell_value_inr=sq * float(r['last_price']),
                     drift_paid_inr=round(sq * (float(r['last_price']) - float(after[0]['close'])), 2))
            table.append((d['symbol'], rd, ld, d['tax_saved_inr'], d['drift_paid_inr'],
                          round(d['tax_saved_inr'] - d['drift_paid_inr'], 2)))
        out.append(d)
    s = summarise(out)
    s['pending'] = pending
    print('curve', json.dumps(ctx['curve']))
    print(json.dumps(s, indent=1))
    print('symbol | call date | LTCG date | saving | drift | net')
    for t in table:
        print(' | '.join(str(x) for x in t))


if __name__ == '__main__':
    main()
