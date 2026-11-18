"""S5 trend-filtered momentum — a ₹50k PAPER book [P-71, trial 265].

Rules are frozen in zerodha-trading docs/reference/s1_momentum/PREREG_S5.md:
S1-T from the S1 kit's backtest.py plus Addendum B (live adaptations). Monthly:
top 10 of the 200 most liquid eligible Nifty-500 names by vol-scaled 6/12-month
momentum, a rank-20 buffer, all cash when Nifty 50 is below its 200-session SMA.
Paper only — this module never places a broker order.
"""
import json
import math
import time
from datetime import date, datetime, timedelta
from fractions import Fraction
from statistics import median

import numpy as np
import pytz

import advisor_paper as ap
import config
import corporate_actions
import database as db
import telegram
from data_jobs import _is_trading_day

IST = pytz.timezone('Asia/Kolkata')
BOOK = 'S5'
INDEX = 'Nifty200 Momentum 30'     # the 2028-12-31 decision benchmark
SEED_KEY, LAST_KEY, TOKENS_KEY = 's5_seed', 's5_last_rebalance', 's5_tokens'
CATCHUP_SESSIONS = 5
SPLIT_BAND = (0.95, 1.05)        # PREREG_S5 Addendum C (k ≈ 1 ± costs unless a corporate action)
DAY_WINDOW = 500                 # calendar days ≈ 340 sessions vs MIN_HIST 260 (Addendum C)
MIN_ELIGIBLE_SHARE = 0.5         # fewer eligible than this × pin = a data failure, not a market
NIFTY_TOKEN = 256265

# Constants copied verbatim from the S1 kit (backtest.py) — a change is a new trial.
N_HOLD, BUFFER, TOP_LIQ = 10, 20, 200
MIN_HIST, MIN_PRICE = 260, 20.0
SLIP = 0.0010                     # 10 bps per leg
STT, STAMP = 0.001, 0.00015
TXN, SEBI, GST = 0.0000307, 0.000001, 0.18
DP = 15.34                        # per scrip per sell day
LIQ_WIN, LIQ_MIN = 126, 100
VOL_WIN, VOL_MIN = 252, 200
R6, R12, SMA = 126, 252, 200


def _day(b) -> str:
    return str(b.get('timestamp') or '')[:10]


def _upto(bars: list, asof: str) -> list:
    return [b for b in bars or [] if _day(b) < asof and b.get('close') is not None]


def _z(x: np.ndarray) -> np.ndarray:
    sd = x.std(ddof=1) if len(x) > 1 else 0.0
    return (x - x.mean()) / sd if sd else np.zeros_like(x)


def rank(bars: dict, nifty: list, asof: str) -> dict:
    """Signal from bars dated strictly before `asof` (the rebalance session).
    Returns {'ranked': top-TOP_LIQ liquid eligible names, best score first,
    'risk_off': Nifty 50 < its 200-SMA, 'eligible': count before the cut}.
    Raises ValueError when Nifty history is too short to decide risk-off."""
    nc = [float(b['close']) for b in _upto(nifty, asof)]
    if len(nc) < SMA:
        raise ValueError(f'nifty history {len(nc)} < {SMA} bars')
    risk_off = nc[-1] < sum(nc[-SMA:]) / SMA

    raw = {s: {_day(b): float(b['close']) for b in _upto(bs, asof)} for s, bs in bars.items()}
    adj = corporate_actions.adjust(raw)
    rows = []
    for s, series in adj.items():
        days = sorted(series)
        if len(days) < MIN_HIST or raw[s][days[-1]] < MIN_PRICE:
            continue
        c = np.array([series[d] for d in days])
        lr = np.diff(np.log(c))[-VOL_WIN:]
        vol = {_day(b): float(b.get('volume') or 0) for b in bars[s]}
        val = [raw[s][d] * vol.get(d, 0.0) for d in days[-LIQ_WIN:]]
        if len(lr) < VOL_MIN or len(val) < LIQ_MIN:
            continue
        sigma = lr.std(ddof=1) * math.sqrt(252)
        if not sigma:
            continue
        rows.append((s, median(val), (c[-1] / c[-1 - R6] - 1) / sigma,
                     (c[-1] / c[-1 - R12] - 1) / sigma))
    eligible = len(rows)
    rows = sorted(rows, key=lambda r: (-r[1], r[0]))[:TOP_LIQ]
    if not rows:
        return {'ranked': [], 'risk_off': risk_off, 'eligible': eligible}
    score = 0.5 * _z(np.array([r[2] for r in rows])) + 0.5 * _z(np.array([r[3] for r in rows]))
    order = sorted(range(len(rows)), key=lambda i: (-score[i], rows[i][0]))
    return {'ranked': [rows[i][0] for i in order], 'risk_off': risk_off, 'eligible': eligible}


# ── costs (kit charges(), baked into fill prices so the ledger stays net) ────

def _charges(turnover: float, side: str) -> float:
    c = turnover * (STT + TXN + SEBI) + GST * turnover * (TXN + SEBI)
    return c + turnover * STAMP if side == 'buy' else c


def buy_fill(px: float, qty: int) -> float:
    """All-in cost per share: 10 bps slippage + statutory charges."""
    return (qty * px * (1 + SLIP) + _charges(qty * px, 'buy')) / qty


def sell_fill(px: float, qty: int) -> float:
    """Net proceeds per share: 10 bps slippage, charges and the ₹15.34 DP fee."""
    return (qty * px * (1 - SLIP) - _charges(qty * px, 'sell') - DP) / qty


# ── calendar ─────────────────────────────────────────────────────────────────

def _next_session(d: date) -> date:
    d += timedelta(days=1)
    while not _is_trading_day(d):
        d += timedelta(days=1)
    return d


def _month_end_on_or_before(d: date):
    for i in range(40):
        x = d - timedelta(days=i)
        if _is_trading_day(x) and _next_session(x).month != x.month:
            return x
    return None


def pending_rebalance(today: str, last):
    """(month_end, 'run'|'skip') for the latest month-end session not yet done,
    or None. 'skip' once more than CATCHUP_SESSIONS sessions have passed."""
    me = _month_end_on_or_before(date.fromisoformat(today))
    if me is None or me.isoformat() < config.S5_START:
        return None
    if last and str(last.get('date') or '') >= me.isoformat():
        return None
    late, d = 0, me
    while d < date.fromisoformat(today):
        d = _next_session(d)
        late += 1
    return me.isoformat(), ('run' if late <= CATCHUP_SESSIONS else 'skip')


# ── strict reads: a swallowed DB error must never look like "no positions" ──

class FetchError(Exception):
    """A Kite fetch came back empty: retry later, never trade on it."""


def _positions() -> list:
    return (db.supabase.table('advisor_paper_positions').select('*')
            .eq('book', BOOK).execute().data or [])              # raises on failure


def _load(key: str):
    raw = db.get_config_strict(key)                              # raises on failure
    return json.loads(raw) if raw else None


# ── market data ──────────────────────────────────────────────────────────────

def _tokens() -> dict:
    """Nifty-500 pin plus the tokens of names S5 bought (a held name can leave
    the pin at a reconstitution or a rename; Kite keeps its token)."""
    m = {k.split(':', 1)[1]: t for k, t in config.NIFTY500_INSTRUMENT_TOKENS.items()}
    for s, t in (_load(TOKENS_KEY) or {}).items():
        m.setdefault(s, t)
    m['NIFTY 50'] = NIFTY_TOKEN
    return m


def _day_bars(md, sym: str, days: int, tokens: dict) -> list:
    key = f'NSE:{sym}'
    if tokens.get(sym):
        md._instrument_cache[key] = tokens[sym]
    bars = md.get_candles(key, 'day', days) or []      # TokenExpiredError propagates
    time.sleep(config.ADVISOR_UNIVERSE_SCAN_DELAY_MS / 1000)
    return bars


def _bar_1520(md, sym: str, day: str, today: str, tokens: dict):
    """Close of the 15:20 5-minute bar on `day` (the kit's 15:25 price). None =
    bars exist but not that one (halted); FetchError = nothing came back."""
    key = f'NSE:{sym}'
    if tokens.get(sym):
        md._instrument_cache[key] = tokens[sym]
    days = (date.fromisoformat(today) - date.fromisoformat(day)).days + 10
    bars = md.get_candles(key, '5minute', days) or []
    if not bars:
        raise FetchError(f'{sym} 5-minute fetch empty')
    for b in bars:
        ts = str(b.get('timestamp') or '')
        if ts[:10] == day and ts[11:16] == '15:20' and b.get('close') is not None:
            return float(b['close'])
    return None


def _last_close(bars: list, upto: str):
    good = [b for b in bars or [] if _day(b) <= upto and b.get('close') is not None]
    return float(good[-1]['close']) if good else None


# ── splits on held names ─────────────────────────────────────────────────────

def patch_splits(positions: list, bars: dict) -> int:
    """k = entry_price / adjusted close on the entry date. Outside SPLIT_BAND the
    holder's shares changed (split/bonus, or a demerger patched in the series):
    qty × k at entry ÷ k, cost basis kept. Idempotent (k ≈ 1 afterwards).
    Raises when the patch cannot be written, so the run retries."""
    series = corporate_actions.adjust(
        {s: {_day(b): float(b['close']) for b in bs if b.get('close') is not None}
         for s, bs in bars.items()})
    n = 0
    for p in positions:
        c = (series.get(p['symbol']) or {}).get(str(p['entry_date'])[:10])
        if not p.get('is_open') or not c:
            continue
        k = float(p['entry_price']) / c
        if SPLIT_BAND[0] <= k <= SPLIT_BAND[1]:
            continue
        snap = float(Fraction(k).limit_denominator(10))
        f = snap if abs(snap / k - 1) < 0.03 else k
        qty = int(p['qty'])
        new_qty = max(1, round(qty * f))
        entry = float(p['entry_price']) * qty / new_qty
        if not db.update_paper_position(p['id'], {'qty': new_qty, 'entry_price': round(entry, 4)}):
            raise RuntimeError(f"split patch write failed for {p['symbol']}")
        print(f"[s5] split patch {p['symbol']}: {qty} → {new_qty} (k={k:.3f})")
        p.update(qty=new_qty, entry_price=entry)
        n += 1
    return n


# ── rebalance ────────────────────────────────────────────────────────────────

def _rebalance(md, me: str, today: str):
    """One month-end rebalance. Returns `me` on success, None when it must be
    retried (no Nifty bar, empty fetch, thin data, rejected write) — guard
    unwritten. Every abort happens before any buy is written; a retry after
    persisted sells converges (target-state, rank-based)."""
    try:
        return _rebalance_once(md, me, today)
    except (FetchError, ValueError) as e:
        print(f"[s5] rebalance {me} aborted, will retry: {e}")
        return None


def _rebalance_once(md, me: str, today: str):
    positions = _positions()
    held = [p for p in positions if p.get('is_open')]
    tokens = _tokens()
    if _bar_1520(md, 'NIFTY 50', me, today, tokens) is None:
        raise ValueError(f'no Nifty 15:20 bar on {me} (holiday / calendar gap?)')
    oldest = min([str(p['entry_date'])[:10] for p in held] + [me])
    deep = max(DAY_WINDOW, (date.fromisoformat(today) - date.fromisoformat(oldest)).days + 10)
    bars = {}
    for p in held:                                  # deep first: the candle cache keys ignore `days`
        bars[p['symbol']] = _day_bars(md, p['symbol'], deep, tokens)
        if not bars[p['symbol']]:
            raise FetchError(f"held {p['symbol']} day bars empty")
    universe = [k.split(':', 1)[1] for k in config.NIFTY500_INSTRUMENT_TOKENS]
    for s in universe:
        if s not in bars:
            bars[s] = _day_bars(md, s, DAY_WINDOW, tokens)
    r = rank({s: bars[s] for s in universe}, _day_bars(md, 'NIFTY 50', DAY_WINDOW, tokens), me)
    if r['eligible'] < MIN_ELIGIBLE_SHARE * len(universe):
        raise ValueError(f"only {r['eligible']} of {len(universe)} eligible — data, not market")
    patch_splits(held, bars)

    cash = ap._reconstruct_cash(BOOK, positions, config.S5_CAPITAL)
    pos = {s: i + 1 for i, s in enumerate(r['ranked'])}
    sells, buys, skipped, kept = [], [], [], []
    for p in held:
        s, qty = p['symbol'], int(p['qty'])
        px = _bar_1520(md, s, me, today, tokens)
        if r['risk_off'] or px is None or pos.get(s, 10 ** 6) > BUFFER:
            px = px or _last_close(bars[s], me) or float(p['entry_price'])
            fill = sell_fill(px, qty)
            if not ap._close_position(p, fill, me, 'REBALANCE'):
                print(f"[s5] sell write failed for {s} — abort before buys")
                return None
            cash += fill * qty
            sells.append((s, qty, px))
        else:
            kept.append((p, px))
    equity = cash + sum(int(p['qty']) * px for p, px in kept)
    have, new = {p['symbol'] for p, _ in kept}, []
    if not r['risk_off']:
        for s in r['ranked']:
            if len(have) >= N_HOLD:
                break
            if s in have:
                continue
            px = _bar_1520(md, s, me, today, tokens)
            if px is None:
                skipped.append(s)
                continue
            budget = min(equity / N_HOLD, cash / (N_HOLD - len(have)))
            per_share = px * (1 + SLIP) * (1 + STT + STAMP + (TXN + SEBI) * (1 + GST))
            q = math.floor(budget / per_share)
            while q > 0 and q * buy_fill(px, q) > cash:
                q -= 1
            if q < 1:
                continue
            fill = buy_fill(px, q)
            cash -= q * fill
            have.add(s)
            new.append(ap._open_position(BOOK, s, 'S5', {}, q, fill, me))
            buys.append((s, q, px))
    if new and db.insert_paper_positions(new) != len(new):
        print(f"[s5] insert failed for {me} — guard not written, will retry")
        return None
    if buys:
        known = _load(TOKENS_KEY) or {}
        known.update({s: tokens[s] for s, _, _ in buys if tokens.get(s)})
        db.write_config(TOKENS_KEY, json.dumps(known))
    if not _load(SEED_KEY):
        db.write_config(SEED_KEY, json.dumps({
            'capital': config.S5_CAPITAL, 'inception': me,
            'index_inception': ap._index_close(me, INDEX)}))
    db.write_config(LAST_KEY, json.dumps({'date': me, 'status': 'run', 'ran_on': today,
                                          'risk_off': r['risk_off'], 'skipped': skipped}))
    _notify(me, today, r['risk_off'], sells, buys, skipped, cash + sum(q * px for _, q, px in buys)
            + sum(int(p['qty']) * px for p, px in kept))
    return me


def _notify(me, today, risk_off, sells, buys, skipped, equity) -> None:
    fmt = lambda xs: ', '.join(f'{s} {q}@{px:,.2f}' for s, q, px in xs) or '—'
    lines = [f"📈 S5 momentum (paper) — rebalance {me}" + (f" (run {today})" if today != me else '')]
    if risk_off:
        lines.append('Risk-off: Nifty 50 below its 200-day average → all cash')
    lines += [f'SELL: {fmt(sells)}', f'BUY: {fmt(buys)}']
    if skipped:
        lines.append(f"Skipped (no 15:20 bar): {', '.join(skipped)}")
    lines.append(f'Equity ≈ ₹{equity:,.0f} · paper only, no orders sent')
    telegram.send_message(config.ADVISOR_TELEGRAM_BOT_TOKEN, config.ADVISOR_TELEGRAM_CHAT_ID,
                          '\n'.join(lines))


# ── daily snapshot ───────────────────────────────────────────────────────────

def _snapshot(md, today: str, seed: dict) -> None:
    positions = _positions()                        # read once: cash and value from one view
    held = [p for p in positions if p.get('is_open')]
    tokens = _tokens()
    oldest = min([str(p['entry_date'])[:10] for p in held] + [today])
    deep = max(DAY_WINDOW, (date.fromisoformat(today) - date.fromisoformat(oldest)).days + 10)
    bars = {p['symbol']: _day_bars(md, p['symbol'], deep, tokens) for p in held}
    patch_splits(held, bars)
    value = sum(int(p['qty']) * (_last_close(bars.get(p['symbol']), today) or float(p['entry_price']))
                for p in held)
    cash = ap._reconstruct_cash(BOOK, positions, float(seed['capital']))
    idx0, idx = float(seed.get('index_inception') or 0), ap._index_close(today, INDEX)
    nifty = _last_close(_day_bars(md, 'NIFTY 50', 10, tokens), today)
    db.upsert_paper_equity({
        'book': BOOK, 'snapshot_date': today,
        'cash': round(cash, 2), 'positions_value': round(value, 2),
        'total_equity': round(cash + value, 2),
        'baseline_equity': round(float(seed['capital']) * idx / idx0, 2) if idx0 and idx else None,
        'nifty_level': round(nifty, 2) if nifty else None,
        'open_positions': len(held),
    })
    print(f"[s5] snapshot {today}: equity ₹{cash + value:,.0f} ({len(held)} open)")


def run_s5(md, today: str = None) -> dict:
    """Post-close entry: rebalance if a month-end is pending, then snapshot.
    DB read failures raise (the scheduler retries in its window)."""
    out = {'rebalanced': None, 'snapshot': False}
    if not config.S5_PAPER_ENABLED:
        return out
    today = today or datetime.now(IST).date().isoformat()
    pend = pending_rebalance(today, _load(LAST_KEY))
    if pend and pend[1] == 'skip':
        db.write_config(LAST_KEY, json.dumps({'date': pend[0], 'status': 'skipped'}))
        print(f"[s5] month-end {pend[0]} missed beyond {CATCHUP_SESSIONS} sessions — skipped")
    elif pend:
        out['rebalanced'] = _rebalance(md, pend[0], today)
    seed = _load(SEED_KEY)
    if seed:
        _snapshot(md, today, seed)
        out['snapshot'] = True
    return out
