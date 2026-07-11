"""Portfolio Advisor — daily HOLD/SELL guidance for the real long-term holdings.

ADVISORY ONLY. This module never places, modifies, or cancels orders — it reads
holdings + daily candles and writes recommendations to portfolio_advice. The
human decides.

Core principle: the entry price is a sunk cost. The verdict comes from the
stock's DIRECTION (daily-timeframe trend structure); position economics only
shape the exit tactics (and the honesty of the breakeven math — a −40% position
needs +67% just to get back to even, which is a claim about the future the
chart has to justify).

Verdicts:
  HOLD            — uptrend intact. Comes with a stop line: hold while above it.
  TRIM            — mixed/sideways structure: de-risk, book part.
  SELL_ON_BOUNCE  — downtrend but oversold near support: don't panic-sell the
                    low; a bounce target is given, sell into it.
  SELL            — confirmed downtrend, no support nearby: cutting is right;
                    holding is bleeding capital that works harder elsewhere.
  INSUFFICIENT    — not enough daily history to say anything honest.
"""
from datetime import datetime

import pytz

import database as db
from indicators import run_all_indicators

IST = pytz.timezone('Asia/Kolkata')

MIN_DAILY_BARS = 60
SWING_LOOKBACK = 20
NEAR_SUPPORT_PCT = 3.0
OVERSOLD_RSI = 32.0


def trend_score(ind: dict, closes: list) -> int:
    """Daily-timeframe direction score in [-100, 100]. Positive = up structure."""
    price = ind.get('current_close') or 0
    score = 0
    ema200 = ind.get('ema_200')
    ema50 = ind.get('ema_50')
    if price and ema200:
        score += 25 if price > ema200 else -25
    if price and ema50:
        score += 20 if price > ema50 else -20
    if ema50 and ema200:
        score += 15 if ema50 > ema200 else -15

    # 20-bar momentum, scaled ±20 (capped at ±6%)
    if len(closes) >= 21 and closes[-21]:
        mom = (closes[-1] - closes[-21]) / closes[-21] * 100
        score += int(max(-20, min(20, mom / 6 * 20)))

    # Directional pressure only when the trend is real (ADX >= 20)
    adx = ind.get('adx')
    if adx and adx >= 20:
        plus, minus = ind.get('adx_plus_di') or 0, ind.get('adx_minus_di') or 0
        score += 10 if plus > minus else -10

    return max(-100, min(100, score))


def swing_levels(candles: list, lookback: int = SWING_LOOKBACK):
    """(support, resistance) from the recent swing window."""
    window = candles[-lookback:]
    lows = [float(c['low']) for c in window if c.get('low') is not None]
    highs = [float(c['high']) for c in window if c.get('high') is not None]
    return (min(lows) if lows else None, max(highs) if highs else None)


def breakeven_gain_pct(avg_price: float, last_price: float):
    """The gain % needed from here just to break even. The number loss-holders
    ignore: −40% needs +66.7%."""
    if not avg_price or not last_price or last_price >= avg_price:
        return 0.0
    return round((avg_price / last_price - 1) * 100, 1)


def tradebook_stats(rows: list) -> dict:
    """Per-symbol behaviour stats from the real tradebook: how often you've
    traded a name and (approximately) how it went. realized_pnl matches sold
    qty against the running average buy cost — an honest approximation, not a
    FIFO tax computation."""
    out = {}
    for r in rows or []:
        try:
            sym = r['symbol']
            qty = float(r['quantity'] or 0)
            price = float(r['price'] or 0)
            s = out.setdefault(sym, {
                'trades': 0, 'buy_qty': 0.0, 'buy_value': 0.0,
                'sell_qty': 0.0, 'realized_pnl': 0.0, 'last_trade_date': None,
            })
            s['trades'] += 1
            s['last_trade_date'] = r.get('trade_date') or s['last_trade_date']
            if (r.get('trade_type') or '').lower() == 'buy':
                s['buy_qty'] += qty
                s['buy_value'] += qty * price
            else:
                avg_cost = (s['buy_value'] / s['buy_qty']) if s['buy_qty'] else price
                s['realized_pnl'] += qty * (price - avg_cost)
                s['sell_qty'] += qty
        except Exception:
            continue
    for s in out.values():
        s['realized_pnl'] = round(s['realized_pnl'], 2)
    return out


def advise(holding: dict, daily_candles: list, history: dict = None) -> dict:
    """One holding → one verdict. Pure: no I/O, no orders.

    holding: {symbol, quantity, average_price, last_price}
    daily_candles: list of {open,high,low,close,volume,timestamp}, oldest first.
    history: optional per-symbol tradebook_stats entry — your own past
    behaviour on this name, folded into the reasons.
    """
    symbol = holding.get('symbol')
    qty = holding.get('quantity') or 0
    avg = float(holding.get('average_price') or 0)
    last = float(holding.get('last_price') or 0)
    pnl_pct = round((last / avg - 1) * 100, 2) if avg and last else None

    base = {
        'symbol': symbol, 'quantity': qty, 'avg_price': avg,
        'last_price': last, 'pnl_percent': pnl_pct,
        'breakeven_gain_pct': breakeven_gain_pct(avg, last),
    }

    if not daily_candles or len(daily_candles) < MIN_DAILY_BARS:
        return {**base, 'verdict': 'INSUFFICIENT', 'confidence': 0,
                'trend_score': 0, 'reasons':
                [f'Only {len(daily_candles or [])} daily bars — need '
                 f'{MIN_DAILY_BARS}+ for an honest read'],
                'stop_level': None, 'exit_target': None, 'indicators': {}}

    ind = run_all_indicators(daily_candles)
    closes = [float(c['close']) for c in daily_candles
              if c.get('close') is not None]
    score = trend_score(ind, closes)
    support, resistance = swing_levels(daily_candles)
    rsi = ind.get('rsi_14')
    price = last or ind.get('current_close') or 0

    near_support = (support is not None and price and
                    (price - support) / price * 100 <= NEAR_SUPPORT_PCT)
    oversold = rsi is not None and rsi <= OVERSOLD_RSI

    reasons = []
    ema50, ema200 = ind.get('ema_50'), ind.get('ema_200')
    if ema200 and price:
        reasons.append(f"Price {'above' if price > ema200 else 'below'} "
                       f"200-day EMA (₹{ema200:.2f})")
    if ema50 and ema200:
        reasons.append(f"50-day EMA {'above' if ema50 > ema200 else 'below'} "
                       f"200-day — {'up' if ema50 > ema200 else 'down'} structure")
    if rsi is not None:
        reasons.append(f"RSI {rsi:.0f}" + (' — oversold' if oversold else ''))
    if pnl_pct is not None and pnl_pct < 0 and base['breakeven_gain_pct'] > 15:
        reasons.append(f"Down {abs(pnl_pct):.0f}% — needs "
                       f"+{base['breakeven_gain_pct']:.0f}% from here just to "
                       f"break even; the chart must justify that")

    # Your own history on this name (real tradebook), when we have it
    if history and history.get('trades'):
        realized = history.get('realized_pnl') or 0.0
        line = (f"Your history here: {history['trades']} fills, realized "
                f"{'+' if realized >= 0 else '−'}₹{abs(realized):,.0f}")
        if realized < 0 and pnl_pct is not None and pnl_pct < 0:
            line += " — this name has cost you both realized and unrealized"
        reasons.append(line)

    if score >= 20:
        verdict = 'HOLD'
        stop = support
        target = None
        reasons.insert(0, 'Uptrend intact on the daily — direction is with you')
        if stop:
            reasons.append(f"Hold while above ₹{stop:.2f} (swing support); "
                           f"a daily close below it is the exit signal")
    elif score <= -20:
        if oversold and near_support:
            verdict = 'SELL_ON_BOUNCE'
            stop = support
            bounce = ind.get('ema_21')
            target = (bounce if bounce and bounce > price else resistance)
            reasons.insert(0, 'Downtrend, but oversold at support — selling the '
                              'panic low is the worst exit')
            if target:
                reasons.append(f"Sell into strength near ₹{target:.2f}; "
                               f"abandon if support ₹{support:.2f} breaks first")
        else:
            verdict = 'SELL'
            stop = None
            target = None
            reasons.insert(0, 'Confirmed downtrend, no support nearby — the '
                              'entry price is sunk cost; holding here is a bet '
                              'against the trend')
    else:
        verdict = 'TRIM'
        stop = support
        target = None
        reasons.insert(0, 'Mixed structure — neither trend has control; '
                          'de-risk by booking part')
        if stop:
            reasons.append(f"Keep the rest only while ₹{stop:.2f} holds")

    return {
        **base,
        'verdict': verdict,
        'confidence': min(90, 50 + abs(score) // 2),
        'trend_score': score,
        'reasons': reasons,
        'stop_level': round(stop, 2) if stop else None,
        'exit_target': round(target, 2) if target else None,
        'indicators': {
            'rsi_14': rsi, 'ema_50': ema50, 'ema_200': ema200,
            'adx': ind.get('adx'), 'atr_14': ind.get('atr_14'),
            'support': support, 'resistance': resistance,
            'daily_bars': ind.get('candle_count'),
            'history': history or None,
        },
    }


def sync_tradebook(kite) -> int:
    """Append today's REAL account trades (GET /trades) into tradebook —
    keeps the imported history current going forward. Read-only; dedup makes
    re-runs safe."""
    try:
        trades = kite.get_account_trades() or []
    except Exception as e:
        print(f"[advisor] tradebook sync failed (non-fatal): {e}")
        return 0
    rows = []
    for t in trades:
        try:
            rows.append({
                'symbol': t.get('tradingsymbol'),
                'isin': None,
                'trade_date': (t.get('fill_timestamp') or
                               t.get('exchange_timestamp') or '')[:10] or None,
                'exchange': t.get('exchange') or 'NSE',
                'segment': 'EQ',
                'series': None,
                'trade_type': (t.get('transaction_type') or '').lower(),
                'quantity': t.get('quantity') or 0,
                'price': t.get('average_price') or 0,
                'trade_id': str(t.get('trade_id') or ''),
                'order_id': str(t.get('order_id') or ''),
                'executed_at': t.get('fill_timestamp') or t.get('exchange_timestamp'),
                'source': 'kite_daily',
            })
        except Exception:
            continue
    rows = [r for r in rows if r['symbol'] and r['trade_id']]
    n = db.upsert_tradebook(rows)
    if n:
        print(f"[advisor] tradebook: appended {n} fills from today")
    return n


def run_advisor(market_data) -> int:
    """Analyze every real holding and store today's advice. ADVISORY ONLY —
    reads holdings + candles, writes portfolio_advice, places nothing.
    Returns rows stored. Per-symbol failures skip that symbol, never abort
    the run."""
    try:
        holdings = market_data.kite.get_holdings() or []
    except Exception as e:
        print(f"[advisor] holdings fetch failed: {e}")
        return 0
    if not holdings:
        print("[advisor] no holdings")
        return 0

    # Keep the real tradebook current, then load your per-symbol history so
    # verdicts can reference how this name has actually treated you.
    sync_tradebook(market_data.kite)
    history = tradebook_stats(db.get_tradebook())

    run_date = datetime.now(IST).date().isoformat()
    rows = []
    for h in holdings:
        tsym = h.get('tradingsymbol')
        if not tsym or (h.get('quantity') or 0) <= 0:
            continue
        exch = h.get('exchange') or 'NSE'
        try:
            candles = market_data.get_candles(f'{exch}:{tsym}', 'day', 400)
            advice = advise({
                'symbol': tsym,
                'quantity': h.get('quantity'),
                'average_price': h.get('average_price'),
                'last_price': h.get('last_price'),
            }, candles or [], history=history.get(tsym))
            rows.append({'run_date': run_date, **advice})
            print(f"[advisor] {tsym}: {advice['verdict']} "
                  f"(trend {advice['trend_score']}, conf {advice['confidence']})")
        except Exception as e:
            print(f"[advisor] {tsym} failed (skipped): {e}")

    n = db.upsert_portfolio_advice(rows)
    print(f"[advisor] stored {n} recommendations for {run_date}")
    return n
