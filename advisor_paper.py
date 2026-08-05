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
import database as db

IST = pytz.timezone('Asia/Kolkata')

MANAGEMENT = 'MANAGEMENT'
PICKING = 'PICKING'
_SEED_MGMT_KEY = 'advisor_paper_seed_mgmt'
_SEED_PICK_KEY = 'advisor_paper_seed_pick'


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
    nothing. Acts on the current open MANAGEMENT positions."""
    open_by_sym = {}
    for p in db.paper_positions(MANAGEMENT, open_only=True):
        open_by_sym.setdefault(p['symbol'], p)
    new_positions = []
    for r in rows:
        sym = r.get('symbol')
        verdict = (r.get('verdict') or '').upper()
        pos = open_by_sym.get(sym)
        px = _last_close(md, sym, cache, fallback=r.get('last_price'))
        if pos and verdict in ('SELL', 'SELL_ON_BOUNCE'):
            _close_position(pos, px, run_date, 'SELL_VERDICT')
        elif pos and verdict == 'TRIM':
            half = int(pos['qty']) // 2
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
                db.update_paper_position(pos['id'], {'qty': int(pos['qty']) - half})
        # rotation: sell freed qty from the holding, buy the target name
        tgt = r.get('rotation_target_symbol')
        sell_qty = int(r.get('rotation_sell_qty') or 0)
        if pos and tgt and sell_qty > 0 and config.ROTATION_ADVISOR_ENABLED:
            e = float(pos['entry_price'])
            sell_qty = min(sell_qty, int(pos['qty']))
            rot_out = _open_position(MANAGEMENT, sym, pos['source'], r,
                                     sell_qty, e, pos['entry_date'])
            rot_out.update({'is_open': False, 'exit_price': px, 'exit_date': run_date,
                            'exit_reason': 'ROTATION_OUT',
                            'realized_pnl': round((px - e) * sell_qty, 2),
                            'return_pct': round((px - e) / e * 100, 3) if e else 0.0})
            new_positions.append(rot_out)
            db.update_paper_position(pos['id'], {'qty': max(0, int(pos['qty']) - sell_qty)})
            buy_px = float(r.get('rotation_buy_price') or _last_close(md, tgt, cache))
            buy_qty = int(r.get('rotation_buy_qty') or 0) or (
                int((px * sell_qty) / buy_px) if buy_px else 0)
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


# ── snapshot ─────────────────────────────────────────────────────────────────

def _snapshot(book: str, run_date: str, md, cache: dict) -> None:
    positions = db.paper_positions(book)
    seed_cash = (config.ADVISOR_PAPER_PICKING_CAPITAL if book == PICKING else 0.0)
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
    return True
