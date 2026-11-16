"""[P-60] Pure daily scoring of one advisor call against the Nifty.

A call is scored on each Nifty trading day after its run_date (td 1..HORIZON).
A day on which the call's symbol has no bar is a gap: it uses up a td but
emits no row. Positive signed alpha = the call is winning that day.
"""
INDEX = 'NIFTY 50'
HORIZON = 60
CHECKPOINTS = (5, 10, 20, 40, 60)
PAIR_COST_PCT = 0.3           # STT + brokerage + slippage, round trip
SHOULD_HAVE_SOLD_PCT = -10.0

_SELLS = ('SELL', 'SELL_ON_BOUNCE', 'TRIM')
_HOLDS = ('HOLD', 'BUY')


def direction(verdict: str) -> int:
    return -1 if verdict in _SELLS else 1 if verdict in _HOLDS else 0


def _base(series: dict, day: str):
    if day in series:
        return series[day]
    before = [d for d in series if d < day]
    return series[max(before)] if before else None


def score_call(call: dict, bars: dict, nifty_days: list, prior: dict = None) -> list:
    """One row per scored day. `prior` = {'td','date','mae','mfe'} continues a
    call already stored: running min/max carry on, only later days are emitted."""
    nifty, sym = bars.get(INDEX, {}), bars.get(call['symbol'], {})
    n0, entry = _base(nifty, call['run_date']), call['entry']
    tgt = bars.get(call['target'], {}) if call.get('target') else {}
    t_entry = call.get('target_entry')
    if not n0 or not entry:
        return []
    mae = prior['mae'] if prior else None
    mfe = prior['mfe'] if prior else None
    after = prior['date'] if prior else None
    rows, td = [], 0
    for day in nifty_days:
        if day <= call['run_date']:
            continue
        td += 1
        if td > HORIZON:
            break
        if (after and day <= after) or day not in sym:
            continue
        ret = (sym[day] / entry - 1) * 100
        npct = (nifty[day] / n0 - 1) * 100
        signed = round(call['direction'] * (ret - npct), 2)
        mae = signed if mae is None else min(mae, signed)
        mfe = signed if mfe is None else max(mfe, signed)
        pair = None
        if day in tgt and t_entry:
            pair = round((tgt[day] / t_entry - 1) * 100 - ret - PAIR_COST_PCT, 2)
        rows.append({'source': call['source'], 'account': call['account'], 'run_date': call['run_date'],
                     'symbol': call['symbol'], 'td': td, 'date': day, 'close': sym[day],
                     'ret_pct': round(ret, 2), 'nifty_pct': round(npct, 2), 'signed_alpha_pct': signed,
                     'mae_pct': mae, 'mfe_pct': mfe, 'pair_pct': pair})
    return rows


def summarize(rows: list, direction: int) -> dict:
    rows = sorted(rows, key=lambda r: r['td'])
    at = {k: None for k in CHECKPOINTS}
    for r in rows:
        if r['td'] in at:
            at[r['td']] = float(r['signed_alpha_pct'])
    if not rows:
        return {'days': 0, 'signed_alpha_pct': None, 'mae_pct': None, 'mfe_pct': None, 'correct': False,
                'early_wrong_days': 0, 'should_have_sold': False, 'at': at}
    last = rows[-1]
    first_right = next((i for i, r in enumerate(rows) if float(r['signed_alpha_pct']) > 0), len(rows))
    mae = min(float(r['mae_pct']) for r in rows)
    return {'days': int(last['td']), 'signed_alpha_pct': float(last['signed_alpha_pct']),
            'mae_pct': mae, 'mfe_pct': max(float(r['mfe_pct']) for r in rows),
            'correct': float(last['signed_alpha_pct']) > 0, 'early_wrong_days': first_right,
            'should_have_sold': direction > 0 and mae <= SHOULD_HAVE_SOLD_PCT, 'at': at}
