"""Advisor paper-portfolio engine (P-14 phase 2).

Two virtual books that ACT on the advisor's daily verdicts so we can measure
wins/losses over time, not just per-call alpha (that's advisor_backtest):

  MANAGEMENT — seeded once from the real holdings; each official run applies
    HOLD / SELL / TRIM / rotation. Baseline = the same holdings frozen at seed
    (the do-nothing counterfactual). Answers "did following the advisor beat
    leaving the portfolio alone?"
  PICKING — starts with fixed cash; buys the rotation/scan TARGET names the
    advisor surfaces, sizes them under a single-name cap, and closes each at a
    fixed horizon for a clean win/loss. Baseline = the same cash in Nifty
    (buy-and-hold). Answers "is the advisor a good stock-picker?"

Advisory-only — never touches orders or the trade loop. Runs on each official
advisor pass; read-only market data (daily candles). Cash is reconstructed from
the position ledger each run (no separate running-balance store to drift):
  cash = seed_cash + Σ(proceeds of every close/trim) − Σ(cost of every non-seed buy)
Idempotent per run_date: verdicts are applied only once (guarded on whether an
equity snapshot already exists for the book+date); re-runs just refresh MTM.
"""
import json
from datetime import datetime

import pytz

import config
import costs
import database as db

IST = pytz.timezone('Asia/Kolkata')

MANAGEMENT = 'MANAGEMENT'
PICKING = 'PICKING'
_SEED_MGMT_KEY = 'advisor_paper_seed_mgmt'
_SEED_PICK_KEY = 'advisor_paper_seed_pick'
PICKING_RP = 'PICKING_RP'
_SEED_RP_KEY = 'advisor_paper_seed_rp'
_RP_LAST_KEY = 'advisor_paper_rp_last_rebalance'   # month guard: a no-trade rebalance still counts


# ── market-data helpers ──────────────────────────────────────────────────────

def _candles(md, symbol: str, cache: dict) -> list:
    """Daily candles for a symbol, cached per run. Warms the instrument token
    from the Nifty-500 map first (a fresh MarketData has an empty cache, so an
    off-map holding would otherwise fetch nothing)."""
    if symbol in cache:
        return cache[symbol]
    key = f'NSE:{symbol}'
    try:
        token = (config.NIFTY500_INSTRUMENT_TOKENS.get(key)
                 or md._instrument_cache.get(key))
        if token:
            md._instrument_cache[key] = token
        cache[symbol] = md.get_candles(key, 'day', 400) or []
    except Exception as e:
        print(f"[paper] candles {symbol}: {e}")
        cache[symbol] = []
    return cache[symbol]


def _last_close(md, symbol: str, cache: dict, fallback: float = 0.0) -> float:
    bars = _candles(md, symbol, cache)
    if bars and bars[-1].get('close') is not None:
        return float(bars[-1]['close'])
    return float(fallback or 0.0)


def _nifty_level(md, cache: dict) -> float:
    try:
        md._instrument_cache['NSE:NIFTY 50'] = 256265
        bars = md.get_candles('NSE:NIFTY 50', 'day', 400) or []
        cache['__nifty__'] = bars
        if bars and bars[-1].get('close') is not None:
            return float(bars[-1]['close'])
    except Exception as e:
        print(f"[paper] nifty level: {e}")
    return 0.0


def _bars_after(candles: list, date_str: str) -> list:
    return [c for c in candles or []
            if str(c.get('timestamp') or '')[:10] >= date_str
            and c.get('close') is not None]


# ── cash reconstruction ──────────────────────────────────────────────────────

def _reconstruct_cash(book: str, positions: list, seed_cash: float) -> float:
    """cash = seed_cash + Σ(proceeds of closes/trims) − Σ(cost of non-seed buys).
    Seed positions are the starting portfolio, not a cash outflow, so they don't
    subtract; only ROTATION/SCAN buys spend cash."""
    cash = float(seed_cash)
    for p in positions:
        if not p.get('is_open') and p.get('exit_price') is not None:
            cash += float(p['exit_price']) * int(p['qty'])
        if p.get('source') != 'SEED':
            cash -= float(p['entry_price']) * int(p['qty'])
    return cash


# ── seeding ──────────────────────────────────────────────────────────────────

def _seed_management(rows: list, run_date: str, md, cache: dict) -> None:
    """Seed the MANAGEMENT book from the official run's holdings + persist the
    frozen seed composition (for the do-nothing baseline)."""
    seed = {}
    positions = []
    for r in rows:
        sym = r.get('symbol')
        qty = int(r.get('quantity') or 0)
        price = float(r.get('avg_price') or 0)
        if not sym or qty <= 0 or price <= 0:
            continue
        seed[sym] = {'qty': qty, 'price': price}
        positions.append({
            'book': MANAGEMENT, 'symbol': sym, 'source': 'SEED',
            'source_advice_id': r.get('id'), 'verdict': r.get('verdict'),
            'qty': qty, 'entry_price': price, 'entry_date': run_date,
            'is_open': True,
        })
    if not positions:
        return
    db.insert_paper_positions(positions)
    db.write_config(_SEED_MGMT_KEY, json.dumps({
        'seed': seed, 'inception_date': run_date,
        'nifty_inception': _nifty_level(md, cache),
    }))
    print(f"[paper] seeded MANAGEMENT: {len(positions)} holdings")


def _seed_picking(run_date: str, md, cache: dict) -> None:
    db.write_config(_SEED_PICK_KEY, json.dumps({
        'capital': config.ADVISOR_PAPER_PICKING_CAPITAL,
        'inception_date': run_date,
        'nifty_inception': _nifty_level(md, cache),
    }))
    print(f"[paper] seeded PICKING: ₹{config.ADVISOR_PAPER_PICKING_CAPITAL:,.0f} cash")


# ── verdict application ──────────────────────────────────────────────────────

def _open_position(book, sym, source, row, qty, price, run_date):
    return {
        'book': book, 'symbol': sym, 'source': source,
        'source_advice_id': row.get('id'), 'verdict': row.get('verdict'),
        'qty': int(qty), 'entry_price': float(price), 'entry_date': run_date,
        'is_open': True,
    }


def _close_position(pos: dict, price: float, run_date: str, reason: str) -> None:
    qty = int(pos['qty'])
    entry = float(pos['entry_price'])
    pnl = (float(price) - entry) * qty
    ret = (float(price) - entry) / entry * 100 if entry else 0.0
    db.update_paper_position(pos['id'], {
        'exit_price': float(price), 'exit_date': run_date,
        'exit_reason': reason, 'realized_pnl': round(pnl, 2),
        'return_pct': round(ret, 3), 'is_open': False,
    })


def _apply_management(rows: list, run_date: str, md, cache: dict) -> None:
    """SELL → close, TRIM → close half (book the win/loss on the trimmed lot,
    keep the rest), rotation → sell the freed qty and buy the target, HOLD →
    nothing. Acts on the current open MANAGEMENT positions.

    A name can carry a SELL/TRIM verdict *and* a rotation target in the same
    run — the rotation is the same shares leaving, not a second sale. `remaining`
    tracks how many shares are actually still held as the legs apply, so each
    share is realized exactly once (P-24: the 08-06 seed booked 7 names twice,
    a full SELL_VERDICT close *and* a full ROTATION_OUT close with identical
    P&L, then zeroed the closed row's qty — −₹71,512.79 recorded vs a true
    −₹39,983.84)."""
    open_by_sym = {}
    for p in db.paper_positions(MANAGEMENT, open_only=True):
        open_by_sym.setdefault(p['symbol'], p)
    new_positions = []
    for r in rows:
        sym = r.get('symbol')
        verdict = (r.get('verdict') or '').upper()
        pos = open_by_sym.get(sym)
        px = _last_close(md, sym, cache, fallback=r.get('last_price'))
        remaining = int(pos['qty']) if pos else 0
        if pos and verdict in ('SELL', 'SELL_ON_BOUNCE'):
            _close_position(pos, px, run_date, 'SELL_VERDICT')
            remaining = 0   # every share is realized on the closed row
        elif pos and verdict == 'TRIM':
            half = remaining // 2
            if half > 0:
                # book the trimmed lot as a realized close; shrink the remainder
                trimmed_row = _open_position(MANAGEMENT, sym, pos['source'], r,
                                             half, pos['entry_price'], pos['entry_date'])
                trimmed_row['is_open'] = False
                trimmed_row['exit_price'] = px
                trimmed_row['exit_date'] = run_date
                trimmed_row['exit_reason'] = 'TRIM'
                e = float(pos['entry_price'])
                trimmed_row['realized_pnl'] = round((px - e) * half, 2)
                trimmed_row['return_pct'] = round((px - e) / e * 100, 3) if e else 0.0
                new_positions.append(trimmed_row)
                remaining -= half
                db.update_paper_position(pos['id'], {'qty': remaining})
        # rotation: sell whatever of the freed qty is still held, buy the target
        tgt = r.get('rotation_target_symbol')
        want_sell = int(r.get('rotation_sell_qty') or 0)
        if pos and tgt and want_sell > 0 and config.ROTATION_ADVISOR_ENABLED:
            e = float(pos['entry_price'])
            sell_qty = min(want_sell, remaining)
            if sell_qty > 0:
                rot_out = _open_position(MANAGEMENT, sym, pos['source'], r,
                                         sell_qty, e, pos['entry_date'])
                rot_out.update({'is_open': False, 'exit_price': px, 'exit_date': run_date,
                                'exit_reason': 'ROTATION_OUT',
                                'realized_pnl': round((px - e) * sell_qty, 2),
                                'return_pct': round((px - e) / e * 100, 3) if e else 0.0})
                new_positions.append(rot_out)
                remaining -= sell_qty
                db.update_paper_position(pos['id'], {'qty': remaining})
            # The BUY leg runs either way: when SELL already closed the name the
            # proceeds are real, only the exit row would have been a duplicate.
            # Size it off the intended sell qty, capped at what was held.
            funded_qty = min(want_sell, int(pos['qty']))
            buy_px = float(r.get('rotation_buy_price') or _last_close(md, tgt, cache))
            buy_qty = int(r.get('rotation_buy_qty') or 0) or (
                int((px * funded_qty) / buy_px) if buy_px else 0)
            if buy_qty > 0 and buy_px > 0:
                new_positions.append(
                    _open_position(MANAGEMENT, tgt, 'ROTATION', r, buy_qty, buy_px, run_date))
    if new_positions:
        db.insert_paper_positions(new_positions)


def _apply_picking(rows: list, run_date: str, md, cache: dict) -> None:
    """Buy every distinct rotation/scan TARGET the advisor surfaces, sized under
    the single-name cap, funded from the reconstructed cash balance. Skips names
    already held in the PICKING book."""
    positions = db.paper_positions(PICKING)
    seed_cash = config.ADVISOR_PAPER_PICKING_CAPITAL
    cash = _reconstruct_cash(PICKING, positions, seed_cash)
    held = {p['symbol'] for p in positions if p.get('is_open')}
    cap = config.ADVISOR_PAPER_MAX_SINGLE_NAME_PCT * seed_cash
    picks = []
    seen = set()
    for r in rows:
        tgt = r.get('rotation_target_symbol')
        if not tgt or tgt in held or tgt in seen:
            continue
        seen.add(tgt)
        px = float(r.get('rotation_buy_price') or _last_close(md, tgt, cache))
        if px <= 0:
            continue
        budget = min(cap, cash)
        qty = int(budget / px)
        if qty <= 0:
            continue
        cash -= qty * px
        picks.append(_open_position(PICKING, tgt, 'SCAN', r, qty, px, run_date))
    if picks:
        db.insert_paper_positions(picks)


def _close_matured_picks(run_date: str, md, cache: dict) -> None:
    """Close any open PICKING position that has reached its trading-day horizon,
    at that horizon bar's close — a clean, fixed-holding-period win/loss."""
    horizon = config.ADVISOR_PAPER_PICK_HORIZON_DAYS
    for p in db.paper_positions(PICKING, open_only=True):
        bars = _bars_after(_candles(md, p['symbol'], cache), p['entry_date'])
        if len(bars) >= horizon:
            _close_position(p, float(bars[horizon - 1]['close']), run_date, 'HORIZON')


# ── PICKING_RP: monthly rebalance to the stored capped-HRP targets ──────────

def _nifty500_close(run_date: str) -> float:
    """Latest Nifty 500 close on/before run_date (index_bars lags ~1 trading day)."""
    try:
        res = (db.supabase.table('index_bars').select('close')
               .eq('index_name', 'Nifty 500').lte('trade_date', run_date)
               .order('trade_date', desc=True).limit(1).execute())
        return float(res.data[0]['close']) if res.data else 0.0
    except Exception as e:
        print(f"[paper] nifty500 close: {e}")
        return 0.0


def _seed_rp(rows: list, run_date: str) -> None:
    capital = sum(int(r.get('quantity') or 0) * float(r.get('last_price') or 0) for r in rows)
    if capital <= 0:
        return
    db.write_config(_SEED_RP_KEY, json.dumps({
        'capital': round(capital, 2), 'seed_date': run_date,
        'nifty500_inception': _nifty500_close(run_date)}))
    print(f"[paper] seeded PICKING_RP: ₹{capital:,.0f}")


def _rp_sell(lots: list, n: int, px: float, run_date: str, out: list) -> None:
    """Sell n shares across open lots, oldest first. A partly-sold lot follows the
    TRIM pattern: a closed row for the sold qty, the open row's qty shrinks."""
    for p in sorted(lots, key=lambda p: str(p.get('entry_date'))):
        if n <= 0:
            break
        q = int(p['qty'])
        if n >= q:
            _close_position(p, px, run_date, 'REBALANCE')
            n -= q
            continue
        e = float(p['entry_price'])
        row = _open_position(PICKING_RP, p['symbol'], p['source'], {}, n, e, p['entry_date'])
        row.update({'is_open': False, 'exit_price': px, 'exit_date': run_date,
                    'exit_reason': 'REBALANCE', 'realized_pnl': round((px - e) * n, 2),
                    'return_pct': round((px - e) / e * 100, 3) if e else 0.0})
        out.append(row)
        db.update_paper_position(p['id'], {'qty': q - n})
        n = 0


def _rebalance_rp(run_date: str, md, cache: dict) -> None:
    """First official run of each calendar month: move each name `step` of the way
    from its current shares C to target shares T = floor(target_pct * equity / px),
    i.e. new = C + trunc(step * (T - C)); held names absent from targets have T=0.
    Sells first, buys only from cash. Reads app_config risk_targets_latest as
    written by the advisor run; skips if absent/stale. Cost per side is baked into
    the fill prices of the traded shares only (buy up, sell down), so
    _reconstruct_cash and realised P&L stay net of costs with no extra ledger."""
    anchor = _load(_SEED_RP_KEY)
    if not anchor:
        return
    positions = db.paper_positions(PICKING_RP)
    if (str(_load(_RP_LAST_KEY) or '')[:7] == run_date[:7]
            or any(str(p.get('entry_date'))[:7] == run_date[:7] for p in positions)):
        return
    rt = _load('risk_targets_latest')
    if not rt or rt.get('asof') != run_date or not rt.get('targets'):
        print(f"[paper] PICKING_RP no fresh targets for {run_date} — skip rebalance")
        return
    c = costs.round_trip_cost_pct() / 2 / 100
    step = float(rt.get('step') or 0.5)
    cash = _reconstruct_cash(PICKING_RP, positions, anchor['capital'])
    lots, px = {}, {}
    for p in positions:
        if p.get('is_open'):
            lots.setdefault(p['symbol'], []).append(p)
            px[p['symbol']] = _last_close(md, p['symbol'], cache, fallback=p['entry_price'])
    tpct = {}
    for t in rt['targets']:
        p_ = px.get(t['symbol']) or _last_close(md, t['symbol'], cache)
        if p_ > 0:
            px[t['symbol']] = p_
            tpct[t['symbol']] = float(t['target_pct'])
    equity = cash + sum(int(p['qty']) * px[s] for s, ls in lots.items() for p in ls)
    delta = {}
    for s in set(lots) | set(tpct):
        cur = sum(int(p['qty']) for p in lots.get(s, []))
        tgt = int(tpct.get(s, 0.0) / 100 * equity / px[s])
        d = int(step * (tgt - cur))      # int() truncates toward zero
        if d:
            delta[s] = d
    new = []
    for s, d in delta.items():           # sells first, to fund the buys
        if d < 0:
            _rp_sell(lots[s], -d, px[s] * (1 - c), run_date, new)
            cash += -d * px[s] * (1 - c)
    for t in rt['targets']:              # buys in target order; never past cash
        s = t['symbol']
        d = delta.get(s, 0)
        if d > 0:
            fill = px[s] * (1 + c)
            q = min(d, int(cash / fill))
            if q > 0:
                cash -= q * fill
                new.append(_open_position(PICKING_RP, s, 'RP', {}, q, fill, run_date))
    if new:
        db.insert_paper_positions(new)
    db.write_config(_RP_LAST_KEY, json.dumps(run_date))


# ── snapshot ─────────────────────────────────────────────────────────────────

def _snapshot(book: str, run_date: str, md, cache: dict) -> None:
    positions = db.paper_positions(book)
    seed_cash = (config.ADVISOR_PAPER_PICKING_CAPITAL if book == PICKING else 0.0)
    if book == PICKING_RP:
        seed_cash = float((_load(_SEED_RP_KEY) or {}).get('capital') or 0.0)
    cash = _reconstruct_cash(book, positions, seed_cash)
    pos_value = sum(
        int(p['qty']) * _last_close(md, p['symbol'], cache, fallback=p['entry_price'])
        for p in positions if p.get('is_open'))
    open_n = sum(1 for p in positions if p.get('is_open'))
    nifty = _nifty_level(md, cache)

    baseline = None
    if book == MANAGEMENT:
        anchor = _load(_SEED_MGMT_KEY)
        if anchor:
            baseline = sum(int(v['qty']) * _last_close(md, s, cache, fallback=v['price'])
                           for s, v in anchor.get('seed', {}).items())
    elif book == PICKING_RP:
        anchor = _load(_SEED_RP_KEY)
        if anchor and anchor.get('nifty500_inception'):
            n5 = _nifty500_close(run_date)
            baseline = float(anchor['capital']) * (n5 / float(anchor['nifty500_inception'])) \
                if n5 else float(anchor['capital'])
    else:
        anchor = _load(_SEED_PICK_KEY)
        if anchor and anchor.get('nifty_inception'):
            baseline = float(anchor['capital']) * (nifty / float(anchor['nifty_inception'])) \
                if nifty else float(anchor['capital'])

    db.upsert_paper_equity({
        'book': book, 'snapshot_date': run_date,
        'cash': round(cash, 2), 'positions_value': round(pos_value, 2),
        'total_equity': round(cash + pos_value, 2),
        'baseline_equity': round(baseline, 2) if baseline is not None else None,
        'nifty_level': round(nifty, 2) if nifty else None,
        'open_positions': open_n,
    })
    print(f"[paper] {book} snapshot {run_date}: equity ₹{cash + pos_value:,.0f} "
          f"({open_n} open, cash ₹{cash:,.0f})")


def _load(key: str):
    try:
        raw = db.get_config(key)
        return json.loads(raw) if raw else None
    except Exception:
        return None


# ── entrypoint ───────────────────────────────────────────────────────────────

def run_paper_portfolio(md, run_date: str = None) -> bool:
    """Update both paper books off today's official advice. Called from the
    scheduler right after the official advisor run. Returns True on a full pass."""
    if not config.ADVISOR_PAPER_ENABLED:
        return False
    run_date = run_date or datetime.now(IST).date().isoformat()
    rows = db.get_official_advice_for_date(run_date)
    if not rows:
        print(f"[paper] no official advice for {run_date} — skip")
        return False

    # Warm holdings tokens so MTM candle fetch works from a cold MarketData too
    # (get_candles' /quote token fallback 400s on a retail enctoken — see
    # advisor_backtest). Non-holding rotation/scan targets resolve via the
    # Nifty-500 map in _candles.
    try:
        md.refresh_holdings_cache()
    except Exception as e:
        print(f"[paper] holdings warm failed (non-fatal): {e}")

    cache = {}
    # Seed once (idempotent — only if the book has no positions / no anchor yet).
    if not db.paper_book_exists(MANAGEMENT):
        _seed_management(rows, run_date, md, cache)
    if not _load(_SEED_PICK_KEY):
        _seed_picking(run_date, md, cache)

    # Apply verdicts only once per day (guard on an existing snapshot), so a
    # same-day re-run just refreshes MTM instead of double-acting.
    already = {e['snapshot_date'] for e in db.paper_equity_curve(MANAGEMENT, limit=5)}
    if run_date not in already:
        _apply_management(rows, run_date, md, cache)
        _apply_picking(rows, run_date, md, cache)
    _close_matured_picks(run_date, md, cache)

    _snapshot(MANAGEMENT, run_date, md, cache)
    _snapshot(PICKING, run_date, md, cache)
    try:    # RP book is independent: never let it break MANAGEMENT/PICKING
        if not _load(_SEED_RP_KEY):
            _seed_rp(rows, run_date)
        _rebalance_rp(run_date, md, cache)
        _snapshot(PICKING_RP, run_date, md, cache)
    except Exception as e:
        print(f"[paper] PICKING_RP failed (non-fatal): {e}")
    return True
