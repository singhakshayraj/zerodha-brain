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
import time
from datetime import datetime

import pytz

import config
import database as db
import market_regime
import news_jobs
import telegram
from indicators import calculate_ema_series, run_all_indicators

IST = pytz.timezone('Asia/Kolkata')

MIN_DAILY_BARS = 60
SWING_LOOKBACK = 20
NEAR_SUPPORT_PCT = 3.0
OVERSOLD_RSI = 32.0
OVEREXTENDED_RSI = 75.0
OVEREXTENDED_ABOVE_EMA50_PCT = 15.0
RELATIVE_STRENGTH_LOOKBACK = 20
CONCENTRATION_FLAG_PCT = 25.0
NIFTY50_INDEX_TOKEN = 256265  # NSE:NIFTY 50 — standard Kite instrument token


def trend_consistency(closes: list, lookback: int = 20):
    """% of the last `lookback` closes sitting above the 50-day EMA — a
    steadier read of trend health than a single EMA50-vs-EMA200 snapshot,
    which can flip on one good/bad day near the cross. Returns None if there
    isn't enough EMA history yet."""
    series = calculate_ema_series(closes, 50)
    if len(series) < lookback:
        return None
    # closes and the ema series are aligned from the same end (both trail
    # the input list), so pair the last `lookback` of each.
    tail_closes = closes[-len(series):][-lookback:]
    tail_ema = series[-lookback:]
    above = sum(1 for c, e in zip(tail_closes, tail_ema) if c > e)
    return round(above / lookback * 100, 1)


def relative_strength(closes: list, benchmark_closes: list,
                      lookback: int = RELATIVE_STRENGTH_LOOKBACK):
    """Stock's `lookback`-day return minus the benchmark's — is this name
    actually stronger or weaker than the market, not just up or down with it?
    Returns None if either series lacks the depth (benchmark is best-effort:
    index history can be unavailable)."""
    if (len(closes) < lookback + 1 or not benchmark_closes
            or len(benchmark_closes) < lookback + 1):
        return None
    stock_ret = (closes[-1] - closes[-lookback - 1]) / closes[-lookback - 1] * 100
    bench_ret = ((benchmark_closes[-1] - benchmark_closes[-lookback - 1])
                 / benchmark_closes[-lookback - 1] * 100)
    return round(stock_ret - bench_ret, 2)


def news_sentiment(symbol: str, now_iso: str = None):
    """Average sentiment of the symbol's recent tagged news (from news_events,
    filled by the news collector / backfill). Range −1..1, or None when the
    name has no recent coverage — the scoring term then contributes 0, same
    honest degradation as relative strength."""
    now_iso = now_iso or datetime.now(IST).isoformat()
    rows = db.recent_news_for_symbol(symbol, now_iso, limit=5)
    scores = [float(r['sentiment_score']) for r in rows
              if r.get('sentiment_score') is not None]
    if not scores:
        return None
    return round(sum(scores) / len(scores), 3)


def volume_trend(candles: list, lookback: int = 10):
    """Recent avg volume vs the prior window, on the SAME lookback — rising
    volume underneath a move (up or down) says participation is real, not a
    thin drift. Returns a ratio (>1 = building), or None if too little data."""
    vols = [float(c.get('volume') or 0) for c in candles]
    if len(vols) < lookback * 2:
        return None
    recent = sum(vols[-lookback:]) / lookback
    prior = sum(vols[-lookback * 2:-lookback]) / lookback
    if not prior:
        return None
    return round(recent / prior, 2)


def trend_score(ind: dict, closes: list, consistency=None,
                rel_strength=None, news_sent=None, regime: str = None) -> int:
    """Daily-timeframe direction score in [-100, 100]. Positive = up structure.

    Weights: EMA200 position 20, EMA50 position 15, trend consistency
    (% of last 20 closes above EMA50) 15, 20-bar momentum 20, ADX direction
    10, relative strength vs Nifty 20, news sentiment 10 (clamped by the
    final [-100,100] bound). Optional terms contribute 0 when data is
    unavailable rather than skewing the read.

    regime (market_regime label) reweights ONLY in HIGH_VOLATILITY_PANIC:
    the EMA200 anchor speaks louder and 20-bar momentum quieter — a panic
    tape's short-term slope is its least trustworthy signal. regime=None or
    any other regime is the identity: the score is bit-for-bit what it was
    before this parameter existed."""
    w = market_regime.score_weights_for(regime)
    price = ind.get('current_close') or 0
    score = 0
    ema200 = ind.get('ema_200')
    ema50 = ind.get('ema_50')
    if price and ema200:
        score += int(round(20 * w['ema200'])) if price > ema200 \
            else -int(round(20 * w['ema200']))
    if price and ema50:
        score += 15 if price > ema50 else -15

    if consistency is not None:
        # 100% above EMA50 -> +15, 0% -> -15, 50% -> 0
        score += int(max(-15, min(15, (consistency - 50) / 50 * 15)))

    # 20-bar momentum, scaled ±20 (capped at ±6%)
    if len(closes) >= 21 and closes[-21]:
        mom = (closes[-1] - closes[-21]) / closes[-21] * 100
        cap = 20 * w['momentum']
        score += int(max(-cap, min(cap, mom / 6 * cap)))

    # Directional pressure only when the trend is real (ADX >= 20)
    adx = ind.get('adx')
    if adx and adx >= 20:
        plus, minus = ind.get('adx_plus_di') or 0, ind.get('adx_minus_di') or 0
        score += 10 if plus > minus else -10

    if rel_strength is not None:
        # ±10% relative to Nifty over the lookback -> full ±20 swing
        score += int(max(-20, min(20, rel_strength / 10 * 20)))

    if news_sent is not None:
        # sentiment −1..1 -> ±10; ±0.4 (strong) already saturates the term
        score += int(max(-10, min(10, news_sent / 0.4 * 10)))

    return max(-100, min(100, score))


def classify_trigger(score: int, price: float, ema200: float,
                     rel_strength: float = None) -> str:
    """MACRO vs MICRO: is this call backed by long-horizon evidence, or only
    by short-term terms? Drives the backtest horizon — a 200-day-structure
    call gets 30 trading days to prove out; a momentum/consistency call is
    judged at 10, because that's the timescale it claims to read.

      MACRO: the EMA200 side agrees with the call's direction, or relative
             strength vs Nifty is decisively (>=5pp) on the call's side.
      MICRO: everything else — the long-term structure is against or silent,
             so short-term terms are what fired the verdict.
    """
    if not price or not ema200:
        return 'MICRO'
    bullish = score >= 0
    if (price > ema200) == bullish:
        return 'MACRO'
    if rel_strength is not None and abs(rel_strength) >= 5 \
            and (rel_strength > 0) == bullish:
        return 'MACRO'
    return 'MICRO'


def smoothed_last_price(market_data, instrument_key: str):
    """EMA over the last three 15-min closes — the verdict-time price with
    single-bar opening noise filtered out (one flush or spike can't flip a
    near-support / oversold check by itself). None on any failure; the
    caller falls back to the raw holdings LTP."""
    try:
        candles = market_data.get_candles(instrument_key, '15minute', 3) or []
        closes = [float(c['close']) for c in candles[-3:]
                  if c.get('close') is not None]
        if not closes:
            return None
        # Standard EMA, span 3 (alpha = 0.5), seeded on the oldest close.
        ema = closes[0]
        for c in closes[1:]:
            ema = 0.5 * c + 0.5 * ema
        return round(ema, 2)
    except Exception as e:
        print(f"[advisor] smoothing failed for {instrument_key} "
              f"(raw LTP used): {e}")
        return None


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


def advise(holding: dict, daily_candles: list, history: dict = None,
          nifty_closes: list = None, portfolio_weight_pct: float = None,
          news_sent: float = None, regime: str = None) -> dict:
    """One holding → one verdict. Pure: no I/O, no orders.

    holding: {symbol, quantity, average_price, last_price}
    daily_candles: list of {open,high,low,close,volume,timestamp}, oldest first.
    history: optional per-symbol tradebook_stats entry — your own past
    behaviour on this name, folded into the reasons.
    nifty_closes: optional benchmark daily closes for relative strength.
    portfolio_weight_pct: optional % of total holdings value this position is.
    regime: optional market_regime label — reweights the score in a panic
    tape and is stored on the row; None behaves exactly as before.
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
                'stop_level': None, 'exit_target': None, 'indicators': {},
                'market_regime': regime, 'trigger_type': None}

    ind = run_all_indicators(daily_candles)
    closes = [float(c['close']) for c in daily_candles
              if c.get('close') is not None]
    consistency = trend_consistency(closes)
    rel_strength = relative_strength(closes, nifty_closes or [])
    vol_trend = volume_trend(daily_candles)
    score = trend_score(ind, closes, consistency=consistency,
                        rel_strength=rel_strength, news_sent=news_sent,
                        regime=regime)
    support, resistance = swing_levels(daily_candles)
    rsi = ind.get('rsi_14')
    price = last or ind.get('current_close') or 0

    near_support = (support is not None and price and
                    (price - support) / price * 100 <= NEAR_SUPPORT_PCT)
    oversold = rsi is not None and rsi <= OVERSOLD_RSI
    ema50, ema200 = ind.get('ema_50'), ind.get('ema_200')
    overextended = (rsi is not None and rsi >= OVEREXTENDED_RSI and ema50
                    and price > ema50 * (1 + OVEREXTENDED_ABOVE_EMA50_PCT / 100))

    reasons = []
    if ema200 and price:
        reasons.append(f"Price {'above' if price > ema200 else 'below'} "
                       f"200-day EMA (₹{ema200:.2f})")
    if ema50 and ema200:
        reasons.append(f"50-day EMA {'above' if ema50 > ema200 else 'below'} "
                       f"200-day — {'up' if ema50 > ema200 else 'down'} structure")
    if consistency is not None:
        reasons.append(f"Held above the 50-day EMA {consistency:.0f}% of the "
                       f"last 20 sessions" if consistency >= 50 else
                       f"Below the 50-day EMA {100 - consistency:.0f}% of the "
                       f"last 20 sessions — trend keeps failing to hold")
    if rel_strength is not None:
        if abs(rel_strength) >= 3:
            reasons.append(
                f"{'Outperforming' if rel_strength > 0 else 'Underperforming'} "
                f"Nifty by {abs(rel_strength):.1f}pp over 20 sessions — "
                f"{'relative strength' if rel_strength > 0 else 'relative weakness'}")
    if rsi is not None:
        tag = ' — oversold' if oversold else (' — overbought/extended' if overextended else '')
        reasons.append(f"RSI {rsi:.0f}{tag}")
    if news_sent is not None and abs(news_sent) >= 0.15:
        reasons.append(
            f"Recent news sentiment {'positive' if news_sent > 0 else 'negative'} "
            f"({news_sent:+.2f}) — the tape's context "
            f"{'supports' if news_sent > 0 else 'works against'} this name")
    if vol_trend is not None and vol_trend >= 1.3:
        reasons.append(f"Volume building ({vol_trend:.1f}× the prior window) "
                       f"— the move has real participation, not a thin drift")
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

    if portfolio_weight_pct is not None and portfolio_weight_pct >= CONCENTRATION_FLAG_PCT:
        reasons.append(f"Concentration: {portfolio_weight_pct:.0f}% of your "
                       f"total holdings value is in this one name — risk "
                       f"management, independent of the trend read")

    if score >= 20:
        if overextended:
            # Don't blindly hold an exhausted rally — the trend is real but
            # stretched far above its own average; book some strength rather
            # than ride a mean-reversion snap with the entire position.
            verdict = 'TRIM'
            stop = ind.get('ema_21') or support
            target = None
            reasons.insert(0, 'Uptrend intact but extended — overbought well '
                              'above the 50-day average; take some off into '
                              'strength rather than risk giving it all back')
        else:
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
        'market_regime': regime,
        'trigger_type': classify_trigger(score, price, ema200, rel_strength),
        'reasons': reasons,
        'stop_level': round(stop, 2) if stop else None,
        'exit_target': round(target, 2) if target else None,
        'indicators': {
            'rsi_14': rsi, 'ema_50': ema50, 'ema_200': ema200,
            'adx': ind.get('adx'), 'atr_14': ind.get('atr_14'),
            'support': support, 'resistance': resistance,
            'daily_bars': ind.get('candle_count'),
            'trend_consistency_pct': consistency,
            'relative_strength_vs_nifty': rel_strength,
            'volume_trend_ratio': vol_trend,
            'news_sentiment': news_sent,
            'portfolio_weight_pct': portfolio_weight_pct,
            'overextended': overextended,
            'history': history or None,
        },
    }


def _sleep(seconds: float) -> None:
    """Indirection so tests can patch out the scan-pacing pause."""
    time.sleep(seconds)


def score_universe(market_data, universe: list = None, nifty_closes: list = None,
                   exclude_symbols: list = None, regime: str = None) -> dict:
    """Daily trend_score for every Nifty 500 name we don't hold — the rotation
    candidate pool. Reuses the exact holdings scorer (same 7 factors minus
    news, which is skipped here: 500 per-symbol news reads for names that
    mostly have no coverage isn't worth the DB round-trips; the term
    contributes 0 either way).

    Paced by config.ADVISOR_UNIVERSE_SCAN_DELAY_MS between candle fetches so
    the once-daily scan (~3 min at 500×350ms) never crowds the paper engine
    sharing this Kite session. Per-symbol failures skip that symbol only.
    Returns {symbol: {'symbol', 'score', 'sector'}} and persists scores to
    stock_universe.advisor_score (a column the paper engine never touches)."""
    universe = config.NIFTY500_UNIVERSE if universe is None else universe
    excl = set(exclude_symbols or [])
    delay_s = max(0.0, config.ADVISOR_UNIVERSE_SCAN_DELAY_MS / 1000.0)
    out = {}
    for entry in universe:
        sym = entry.get('symbol')
        token = entry.get('instrument_token')
        if not sym or not token or sym in excl:
            continue
        try:
            key = f"NSE:{sym}"
            market_data._instrument_cache[key] = token
            candles = market_data.get_candles(key, 'day', 400)
            _sleep(delay_s)
            if not candles or len(candles) < MIN_DAILY_BARS:
                continue
            ind = run_all_indicators(candles)
            closes = [float(c['close']) for c in candles
                      if c.get('close') is not None]
            score = trend_score(
                ind, closes,
                consistency=trend_consistency(closes),
                rel_strength=relative_strength(closes, nifty_closes or []),
                news_sent=None, regime=regime)
            out[sym] = {'symbol': sym, 'score': score,
                        'sector': entry.get('sector'),
                        'last_close': closes[-1] if closes else None}
        except Exception as e:
            print(f"[advisor.scan] {sym} skipped: {e}")
    if out:
        scored_at = datetime.now(IST).isoformat()
        db.upsert_stock_universe_bulk([
            {'symbol': s['symbol'], 'advisor_score': s['score'],
             'advisor_score_updated_at': scored_at} for s in out.values()])
    return out


def find_rotation_candidate(exit_score: int, sector: str, scored: dict,
                            min_gap: int = None, min_target_score: int = None):
    """Best rotation target for a weak holding, or None. Gate (all three must
    hold — rotate into strength, never into least-bad):
      exit_score <= ROTATION_MAX_EXIT_SCORE   (the holding is genuinely weak)
      target score >= ROTATION_MIN_TARGET_SCORE (the target is genuinely strong)
      target - exit >= ROTATION_MIN_GAP         (the upgrade is wide, not noise)
    Same-sector candidates preferred; cross-sector only when none qualify."""
    min_gap = config.ROTATION_MIN_GAP if min_gap is None else min_gap
    min_target = (config.ROTATION_MIN_TARGET_SCORE
                  if min_target_score is None else min_target_score)
    if exit_score is None or exit_score > config.ROTATION_MAX_EXIT_SCORE:
        return None

    def qualifies(c):
        return (c['score'] >= min_target
                and c['score'] - exit_score >= min_gap)

    ranked = sorted(scored.values(), key=lambda c: c['score'], reverse=True)
    for reason, pool in (
            ('same_sector', [c for c in ranked
                             if sector and c.get('sector') == sector]),
            ('cross_sector', ranked)):
        for c in pool:
            if qualifies(c):
                return {'symbol': c['symbol'], 'score': c['score'],
                        'sector': c.get('sector'), 'reason': reason,
                        'last_close': c.get('last_close')}
    return None


# Deploy slightly under the freed capital: Zerodha releases ~80% of CNC sell
# proceeds same-day, and the buy shouldn't assume a perfect fill price.
ROTATION_DEPLOY_FRACTION = 0.95


def size_rotation(verdict: str, qty: int, last_price: float,
                  target_price: float) -> dict:
    """Concrete rotation sizing: how many shares to sell, how much capital
    that frees, and how many target shares it buys. TRIM is a half-exit.
    Pure; empty dict when unsizeable (no qty/price)."""
    if not qty or not last_price:
        return {}
    sell_qty = qty if verdict != 'TRIM' else max(1, qty // 2)
    freed = round(sell_qty * last_price, 2)
    out = {'rotation_sell_qty': sell_qty, 'rotation_freed_inr': freed}
    if target_price and target_price > 0:
        out['rotation_buy_qty'] = int(freed * ROTATION_DEPLOY_FRACTION
                                      // target_price)
        out['rotation_buy_price'] = round(float(target_price), 2)
    return out


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


_ACTIONABLE = ('SELL', 'SELL_ON_BOUNCE', 'TRIM')


def build_digest(rows: list, run_date: str) -> str:
    """Telegram text for the day's ACTIONABLE calls only — HOLDs are noise in
    a push channel. Empty string = nothing worth sending today."""
    act = [r for r in rows or []
           if r.get('verdict') in _ACTIONABLE or r.get('rotation_target_symbol')]
    if not act:
        return ''
    act.sort(key=lambda r: r.get('trend_score') or 0)
    lines = [f"📋 Portfolio Advisor — {run_date}",
             f"{len(act)} actionable of {len(rows)} holdings:"]
    for r in act:
        pnl = r.get('pnl_percent')
        line = (f"\n{r['symbol']}: {r['verdict']} "
                f"(trend {r.get('trend_score')}, "
                f"{'+' if (pnl or 0) >= 0 else ''}{pnl}%)")
        if r.get('exit_target'):
            line += f" — sell near ₹{r['exit_target']}"
        elif r.get('stop_level') and r.get('verdict') == 'TRIM':
            line += f" — keep rest only above ₹{r['stop_level']}"
        if r.get('rotation_target_symbol'):
            line += (f"\n  ↪ rotate into {r['rotation_target_symbol']} "
                     f"(score {r.get('rotation_target_score')}, "
                     f"{'same sector' if r.get('rotation_reason') == 'same_sector' else 'cross-sector'})")
            if r.get('rotation_buy_qty'):
                line += (f"\n  💰 sell {r.get('rotation_sell_qty')} → "
                         f"₹{r.get('rotation_freed_inr'):,.0f} frees "
                         f"~{r.get('rotation_buy_qty')} "
                         f"{r['rotation_target_symbol']} "
                         f"@ ₹{r.get('rotation_buy_price'):,.2f}")
        lines.append(line)
    lines.append("\nAdvisory only — you decide. Full read: /advisor")
    return '\n'.join(lines)


def build_decision_keyboard(rows: list, run_date: str) -> dict:
    """Inline Accept/Decline buttons, one row per actionable call. Callback
    data 'adv|<run_date>|<symbol>|<accept/decline>' — parsed and verified by
    advisor_bot (which records the DECISION only; nothing here or there can
    place an order). None when nothing is actionable."""
    act = [r for r in rows or []
           if r.get('verdict') in _ACTIONABLE or r.get('rotation_target_symbol')]
    if not act:
        return None
    act.sort(key=lambda r: r.get('trend_score') or 0)
    keyboard = [[
        {'text': f"✅ {r['symbol']}",
         'callback_data': f"adv|{run_date}|{r['symbol']}|accept"},
        {'text': f"❌ {r['symbol']}",
         'callback_data': f"adv|{run_date}|{r['symbol']}|decline"},
    ] for r in act]
    return {'inline_keyboard': keyboard}


def send_daily_digest(rows: list, run_date: str) -> bool:
    """One push per day after the advisor run. Dedup is durable (app_config
    'advisor_digest_date') so a manual advisor_run_now re-run doesn't
    double-send. No-ops without the flag + both bot creds. Never raises."""
    try:
        if not (config.ADVISOR_DIGEST_ENABLED
                and config.ADVISOR_TELEGRAM_BOT_TOKEN
                and config.ADVISOR_TELEGRAM_CHAT_ID):
            return False
        if (db.get_config('advisor_digest_date') or '') == run_date:
            return False
        text = build_digest(rows, run_date)
        if not text:
            return False
        markup = (build_decision_keyboard(rows, run_date)
                  if config.ADVISOR_DECISIONS_ENABLED else None)
        if markup:
            text += ("\n\nTap ✅/❌ per call to record your decision — the "
                     "track record then judges accepted and declined calls "
                     "separately. (Recording only; no order is placed.)")
        sent = telegram.send_message(config.ADVISOR_TELEGRAM_BOT_TOKEN,
                                     config.ADVISOR_TELEGRAM_CHAT_ID, text,
                                     reply_markup=markup)
        if sent:
            db.write_config('advisor_digest_date', run_date)
        return sent
    except Exception as e:
        print(f"[advisor] digest failed (non-fatal): {e}")
        return False


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

    # BUG FIX (2026-07-12): get_candles resolves an instrument_token from
    # market_data's caches, but those are only populated by
    # refresh_holdings_cache() — which this path never calls (it reads
    # holdings straight from kite). Every candle fetch silently found no
    # token and returned []  ->  every verdict was INSUFFICIENT ("0 daily
    # bars"). Seed the token cache directly from this holdings response.
    for h in holdings:
        tsym = h.get('tradingsymbol')
        token = h.get('instrument_token')
        if tsym and token:
            key = f"{h.get('exchange') or 'NSE'}:{tsym}"
            market_data._instrument_cache[key] = token

    # Keep the real tradebook current, then load your per-symbol history so
    # verdicts can reference how this name has actually treated you.
    sync_tradebook(market_data.kite)
    history = tradebook_stats(db.get_tradebook())

    # Best-effort Nifty 50 benchmark for relative strength. Index historical
    # candles are usually available even where index /quote is retail-
    # restricted; if this fails for any reason, relative strength is simply
    # skipped per-symbol below — never blocks a verdict.
    nifty_closes = []
    nifty_candles = []
    try:
        market_data._instrument_cache['NSE:NIFTY 50'] = NIFTY50_INDEX_TOKEN
        nifty_candles = market_data.get_candles('NSE:NIFTY 50', 'day', 400) or []
        nifty_closes = [float(c['close']) for c in nifty_candles
                        if c.get('close') is not None]
    except Exception as e:
        print(f"[advisor] nifty benchmark unavailable (non-fatal): {e}")

    # Market Regime Filter: one read of the index tape shapes today's lens —
    # panic reweights the score toward long-term structure, chop widens the
    # rotation gate. Fail-safe NEUTRAL changes nothing.
    regime_info = market_regime.get_market_regime(nifty_candles)
    regime = regime_info['regime']
    print(f"[advisor.regime] {regime} (ADX {regime_info['adx']}, "
          f"ATR% {regime_info['atr_pct']}, "
          f"EMA20 dist {regime_info['ema20_dist_pct']}%)")

    # Refresh news for the PORTFOLIO names (the trading-session collector only
    # covers the trading universe). No-op unless the collector is enabled +
    # keyed; failure never blocks a verdict. The advisor runs outside the
    # trading hot loop, so a synchronous fetch here is fine.
    try:
        news_jobs.collect([
            f"{h['tradingsymbol']}.NS" for h in holdings
            if h.get('tradingsymbol')
        ][:50])
    except Exception as e:
        print(f"[advisor] news refresh failed (non-fatal): {e}")

    # Portfolio concentration: this holding's share of total holdings value,
    # so a fine trend call can still carry a "too much in one name" flag.
    total_value = sum(
        (h.get('quantity') or 0) * (h.get('last_price') or 0) for h in holdings
    )

    run_date = datetime.now(IST).date().isoformat()
    rows = []
    for h in holdings:
        tsym = h.get('tradingsymbol')
        qty = h.get('quantity') or 0
        if not tsym or qty <= 0:
            continue
        exch = h.get('exchange') or 'NSE'
        try:
            key = f'{exch}:{tsym}'
            candles = market_data.get_candles(key, 'day', 400)
            weight_pct = (round(qty * (h.get('last_price') or 0)
                                / total_value * 100, 1) if total_value else None)
            # Verdict-time price: EMA of the last three 15-min closes when
            # available — one opening-bell spike/flush can't flip a
            # near-support or oversold check. Raw LTP on any failure.
            last_price = h.get('last_price')
            if config.ADVISOR_PRICE_SMOOTHING_ENABLED:
                smoothed = smoothed_last_price(market_data, key)
                if smoothed:
                    last_price = smoothed
            advice = advise({
                'symbol': tsym,
                'quantity': qty,
                'average_price': h.get('average_price'),
                'last_price': last_price,
            }, candles or [], history=history.get(tsym),
               nifty_closes=nifty_closes, portfolio_weight_pct=weight_pct,
               news_sent=news_sentiment(tsym), regime=regime)
            rows.append({'run_date': run_date, **advice})
            print(f"[advisor] {tsym}: {advice['verdict']} "
                  f"(trend {advice['trend_score']}, conf {advice['confidence']}, "
                  f"bars {len(candles or [])})")
        except Exception as e:
            print(f"[advisor] {tsym} failed (skipped): {e}")

    # Rotation pass (dark until ROTATION_ADVISOR_ENABLED): scan the Nifty 500
    # for stronger homes for capital stuck in weak holdings. Non-fatal — a
    # scan failure never blocks the day's verdicts.
    if config.ROTATION_ADVISOR_ENABLED and rows:
        try:
            held = {r['symbol'] for r in rows}
            t0 = time.monotonic()
            scored = score_universe(market_data, nifty_closes=nifty_closes,
                                    exclude_symbols=held, regime=regime)
            print(f"[advisor.scan] scored {len(scored)} universe names in "
                  f"{time.monotonic() - t0:.0f}s")
            sector_of = {u['symbol']: u.get('sector')
                         for u in config.NIFTY500_UNIVERSE}
            # Regime-adaptive gate: chop demands a wider score gap before a
            # rotation is worth the churn (65 vs 40 by default).
            min_gap = market_regime.rotation_min_gap_for(regime)
            if min_gap != config.ROTATION_MIN_GAP:
                print(f"[advisor.regime] rotation gap widened to {min_gap} "
                      f"({regime})")
            for row in rows:
                target = find_rotation_candidate(
                    row.get('trend_score'), sector_of.get(row['symbol']),
                    scored, min_gap=min_gap)
                if target:
                    row['rotation_target_symbol'] = target['symbol']
                    row['rotation_target_score'] = target['score']
                    row['rotation_reason'] = target['reason']
                    sizing = size_rotation(
                        row.get('verdict'), row.get('quantity'),
                        row.get('last_price'), target.get('last_close'))
                    row.update(sizing)
                    size_note = ''
                    if sizing.get('rotation_buy_qty'):
                        size_note = (f" Size: sell {sizing['rotation_sell_qty']} "
                                     f"(₹{sizing['rotation_freed_inr']:,.0f}) → "
                                     f"buy ~{sizing['rotation_buy_qty']} "
                                     f"@ ₹{sizing['rotation_buy_price']:,.2f}")
                    row['reasons'] = (row.get('reasons') or []) + [
                        f"Rotation: {target['symbol']} scores "
                        f"{target['score']} vs this name's "
                        f"{row.get('trend_score')} "
                        f"({'same sector' if target['reason'] == 'same_sector' else 'different sector: ' + (target.get('sector') or '?')})"
                        f" — freed capital has a stronger home.{size_note}"]
                    print(f"[advisor.rotation] {row['symbol']} "
                          f"({row.get('trend_score')}) -> {target['symbol']} "
                          f"({target['score']}, {target['reason']})")
        except Exception as e:
            print(f"[advisor] rotation pass failed (non-fatal): {e}")

    n = db.upsert_portfolio_advice(rows)
    print(f"[advisor] stored {n} recommendations for {run_date}")
    send_daily_digest(rows, run_date)   # non-fatal by construction
    return n
