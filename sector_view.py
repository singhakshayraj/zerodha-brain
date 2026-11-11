"""Sector view — display only, never changes a verdict.

Per sector group: your weight vs its Nifty-500 market-cap weight, the median
advisor trend score, the SELL/TRIM/HOLD mix across every scanned name, median
fundamentals quality (and Gross/Net NPA for lenders when pasted), and which
holdings sit in it. Inputs are today's universe_calls (all scanned names) plus
the account's own holdings rows, so the same function serves the primary
account and the preview account.

"Financial Services" is 101 names in the NSE classification; it is split into
sub-groups so PSU banks are not averaged with insurers and AMCs.
"""
from statistics import median

import config

PSU_BANKS = {'SBIN', 'BANKBARODA', 'PNB', 'CANBK', 'UNIONBANK', 'BANKINDIA',
             'INDIANB', 'IOB', 'UCOBANK', 'CENTRALBK', 'MAHABANK', 'PSB'}
PRIVATE_BANKS = {'HDFCBANK', 'ICICIBANK', 'KOTAKBANK', 'AXISBANK', 'INDUSINDBK',
                 'IDFCFIRSTB', 'FEDERALBNK', 'BANDHANBNK', 'RBLBANK', 'YESBANK',
                 'AUBANK', 'KARURVYSYA', 'CUB', 'J&KBANK', 'EQUITASBNK',
                 'UJJIVANSFB', 'DCBBANK', 'SOUTHBANK', 'CSBBANK', 'KTKBANK',
                 'TMB', 'IDBI'}
INSURANCE = {'SBILIFE', 'HDFCLIFE', 'ICICILIFE', 'ICICIGI', 'LICI', 'GICRE',
             'NIACL', 'STARHEALTH', 'MFSL', 'CANHLIFE', 'GODIGIT', 'NIVABUPA'}
CAPITAL_MARKETS = {'HDFCAMC', 'ICICIAMC', 'NAM-INDIA', 'UTIAMC', 'ABSLAMC',
                   'CDSL', 'CAMS', 'KFINTECH', 'BSE', 'MCX', 'ANGELONE',
                   'MOTILALOFS', 'NUVAMA', 'IEX', '360ONE', 'ANANDRATHI',
                   'SBIFM', 'NSDL', 'PRUDENT'}
PSU_LENDERS = {'PFC', 'RECLTD', 'IRFC', 'IREDA', 'HUDCO', 'IFCI'}


def group_of(symbol: str, sector: str) -> str:
    if symbol.endswith('BEES') or 'ETF' in symbol or symbol in ('HDFCSENSEX',):
        return 'ETFs (gold, silver, index)'
    if sector != 'Financial Services':
        return sector or 'Outside Nifty 500'
    for name, members in (('PSU banks', PSU_BANKS), ('Private banks', PRIVATE_BANKS),
                          ('Insurance', INSURANCE), ('Capital markets', CAPITAL_MARKETS),
                          ('PSU lenders', PSU_LENDERS)):
        if symbol in members:
            return name
    return 'NBFC & other finance'


def _bucket(verdict: str) -> str:
    return ('sell' if verdict in ('SELL', 'SELL_ON_BOUNCE')
            else 'trim' if verdict == 'TRIM' else 'hold' if verdict == 'HOLD' else None)


def aggregate(universe_calls: list, holdings: list, store: dict,
              sector_of: dict = None) -> list:
    """Pure. universe_calls: [{symbol, sector, verdict, trend_score, quality}]
    for scanned (non-held) names; holdings: advisor rows {symbol, quantity,
    last_price, verdict, trend_score}; store: fundamentals by_symbol.
    Returns sector groups sorted held-first by your weight, then index weight."""
    sector_of = sector_of if sector_of is not None else {
        u['symbol']: u.get('sector') for u in config.NIFTY500_UNIVERSE}
    by = (store or {}).get('by_symbol') or {}
    groups = {}

    def g(sym):
        name = group_of(sym, sector_of.get(sym))
        return groups.setdefault(name, {'group': name, 'names': 0, 'trend': [],
                                        'quality': [], 'gnpa': [], 'nnpa': [],
                                        'mix': {'sell': 0, 'trim': 0, 'hold': 0},
                                        'mcap': 0.0, 'my_value': 0.0, 'held': []})

    seen = set()
    for c in universe_calls or []:
        x = g(c['symbol']); seen.add(c['symbol'])
        x['names'] += 1
        if c.get('trend_score') is not None:
            x['trend'].append(c['trend_score'])
        b = _bucket(c.get('verdict'))
        if b:
            x['mix'][b] += 1
    total_value = sum((h.get('quantity') or 0) * (h.get('last_price') or 0)
                      for h in holdings or [])
    for h in holdings or []:
        sym = h['symbol']
        x = g(sym)
        value = (h.get('quantity') or 0) * (h.get('last_price') or 0)
        x['my_value'] += value
        x['held'].append({'symbol': sym, 'verdict': h.get('verdict'),
                          'weight_pct': round(value / total_value * 100, 1) if total_value else None})
        if sym not in seen:
            seen.add(sym)
            x['names'] += 1
            if h.get('trend_score') is not None:
                x['trend'].append(h['trend_score'])
            b = _bucket(h.get('verdict'))
            if b:
                x['mix'][b] += 1
    for sym in seen:
        f = by.get(sym) or {}
        x = g(sym)
        if f.get('mcap'):
            x['mcap'] += f['mcap']
        for k in ('gnpa', 'nnpa'):
            if f.get(k) is not None:
                x[k].append(f[k])
    for c in universe_calls or []:
        if c.get('quality') is not None:
            g(c['symbol'])['quality'].append(c['quality'])

    total_mcap = sum(x['mcap'] for x in groups.values())
    out = []
    for x in groups.values():
        n_mix = sum(x['mix'].values())
        out.append({
            'group': x['group'], 'names': x['names'],
            'my_weight_pct': round(x['my_value'] / total_value * 100, 1) if total_value else 0.0,
            'index_weight_pct': round(x['mcap'] / total_mcap * 100, 1) if total_mcap else None,
            'trend_median': round(median(x['trend'])) if x['trend'] else None,
            'sell_pct': round(x['mix']['sell'] / n_mix * 100) if n_mix else None,
            'mix': x['mix'],
            'quality_median': round(median(x['quality'])) if x['quality'] else None,
            'gnpa_median': round(median(x['gnpa']), 2) if x['gnpa'] else None,
            'nnpa_median': round(median(x['nnpa']), 2) if x['nnpa'] else None,
            'held': sorted(x['held'], key=lambda h: -(h['weight_pct'] or 0)),
        })
    out.sort(key=lambda s: (-s['my_weight_pct'], -(s['index_weight_pct'] or 0)))
    return out


def compute(run_date: str, holdings: list) -> dict:
    """Latest universe_calls on or before run_date (the preview can run
    before the day's scan lands) + fundamentals store + these holdings."""
    import database as db
    import fundamentals
    import universe_calls
    latest = (db.supabase.table(universe_calls.TABLE).select('run_date')
              .lte('run_date', run_date).order('run_date', desc=True)
              .limit(1).execute().data) or []
    scan_date = latest[0]['run_date'] if latest else None
    calls = universe_calls._paged(
        lambda: db.supabase.table(universe_calls.TABLE)
        .select('symbol,sector,verdict,trend_score,quality')
        .eq('run_date', scan_date).order('symbol')) if scan_date else []
    return {'run_date': run_date, 'scan_date': scan_date,
            'groups': aggregate(calls, holdings, fundamentals.load_store())}
