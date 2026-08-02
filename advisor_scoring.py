"""Portfolio-advisor scoring core (SE4 module split part 2, P-06).

The PURE per-holding scoring engine, extracted from portfolio_advisor so that
module keeps only the orchestration (score_universe / run_advisor* / rotation /
timeline). No I/O except news_sentiment's news read; no orders. portfolio_advisor
re-exports every public name here, so callers/tests keep using
portfolio_advisor.<name> unchanged.
"""
from datetime import datetime

import pytz

import database as db
import market_regime
from advisor_risk import CONCENTRATION_FLAG_PCT
from indicators import calculate_ema, calculate_ema_series, run_all_indicators

IST = pytz.timezone('Asia/Kolkata')

MIN_DAILY_BARS = 60
SWING_LOOKBACK = 20
NEAR_SUPPORT_PCT = 3.0
OVERSOLD_RSI = 32.0
OVEREXTENDED_RSI = 75.0
OVEREXTENDED_ABOVE_EMA50_PCT = 15.0
RELATIVE_STRENGTH_LOOKBACK = 20
NIFTY50_INDEX_TOKEN = 256265  # NSE:NIFTY 50 — standard Kite instrument token

# Weekly (higher-timeframe) structure — the read a daily-only scorer is
# blind to. ~30 weeks ≈ 150 trading days = the classic long-term weekly
# trend line; 10 weeks is the intermediate fallback for shorter histories.
WEEKLY_EMA_LONG = 30
WEEKLY_EMA_MID = 10
WEEKLY_MOMENTUM_WEEKS = 8


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


def resample_weekly(daily_candles: list) -> list:
    """Group daily bars into weekly OHLCV (ISO week), oldest first. Each
    weekly bar: open=first day's open, high/low=week extremes, close=last
    day's close, volume=summed, timestamp=last day in the week. Pure — the
    daily candles are already fetched, so this is free (no I/O)."""
    from datetime import date
    weeks, order = {}, []
    for c in daily_candles or []:
        ts = str(c.get('timestamp') or '')[:10]
        if not ts or c.get('close') is None:
            continue
        try:
            iso = date.fromisoformat(ts).isocalendar()
        except ValueError:
            continue
        key = (iso[0], iso[1])
        hi, lo = c.get('high'), c.get('low')
        if key not in weeks:
            weeks[key] = {'open': c.get('open'), 'high': hi, 'low': lo,
                          'close': c.get('close'),
                          'volume': c.get('volume') or 0, 'timestamp': ts}
            order.append(key)
        else:
            wk = weeks[key]
            if hi is not None:
                wk['high'] = hi if wk['high'] is None else max(wk['high'], hi)
            if lo is not None:
                wk['low'] = lo if wk['low'] is None else min(wk['low'], lo)
            wk['close'] = c.get('close')
            wk['volume'] += c.get('volume') or 0
            wk['timestamp'] = ts
    return [weeks[k] for k in order]


def weekly_trend(daily_candles: list, price: float) -> dict:
    """Higher-timeframe structure read. UP when price holds above the weekly
    trend EMA with non-negative multi-week momentum; DOWN the mirror;
    SIDEWAYS when the two disagree. weekly_trend=None when there isn't enough
    weekly history — same honest degradation as the other optional terms.

    This does NOT feed the numeric trend_score yet (its weight is unproven —
    same dark-flag discipline the trading engine uses): it's computed,
    logged on every row, and surfaced in the reasons so the human sees a
    daily/weekly conflict, while factor_attribution measures whether the
    alignment actually predicts before it earns a score weight."""
    weekly = resample_weekly(daily_candles)
    closes = [float(w['close']) for w in weekly if w.get('close') is not None]
    empty = {'weekly_trend': None, 'weekly_ema_long': None,
             'weekly_ema_mid': None, 'price_vs_weekly_pct': None,
             'weekly_weeks': len(closes)}
    if len(closes) < WEEKLY_EMA_MID or not price:
        return empty
    ema_long = (calculate_ema(closes, WEEKLY_EMA_LONG)
                if len(closes) >= WEEKLY_EMA_LONG else None)
    ema_mid = calculate_ema(closes, WEEKLY_EMA_MID)
    anchor = ema_long or ema_mid
    if not anchor:
        return empty
    price_vs = round((price - anchor) / anchor * 100, 2)
    mom = (closes[-1] - closes[-WEEKLY_MOMENTUM_WEEKS - 1]
           if len(closes) >= WEEKLY_MOMENTUM_WEEKS + 1 else None)
    above = price > anchor
    if above and (mom is None or mom >= 0):
        label = 'UP'
    elif not above and (mom is None or mom <= 0):
        label = 'DOWN'
    else:
        label = 'SIDEWAYS'
    return {'weekly_trend': label, 'weekly_ema_long': ema_long,
            'weekly_ema_mid': ema_mid, 'price_vs_weekly_pct': price_vs,
            'weekly_weeks': len(closes)}


def daily_weekly_alignment(daily_score: int, weekly_label: str) -> str:
    """How the daily direction (from trend_score) and the weekly structure
    relate — the single most decision-relevant cross-timeframe fact. None
    when the weekly read is unavailable."""
    if not weekly_label:
        return None
    daily_dir = 'UP' if daily_score >= 20 else 'DOWN' if daily_score <= -20 else 'SIDEWAYS'
    if daily_dir == 'SIDEWAYS' or weekly_label == 'SIDEWAYS':
        return 'NEUTRAL'
    if daily_dir == weekly_label:
        return 'ALIGNED_UP' if weekly_label == 'UP' else 'ALIGNED_DOWN'
    return 'CONFLICT'


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


def smoothed_last_price(market_data, instrument_key: str, today: str = None):
    """EMA over TODAY's last (up to three) 15-min closes — the verdict-time
    price with single-bar opening noise filtered out (one flush or spike
    can't flip a near-support / oversold check by itself).

    Strictly same-session: candles from prior days are discarded, never
    blended — smoothing Friday's close into a gapped Monday open would be
    the opposite of this feature's purpose. None when today has no closed
    bar yet or on any failure; the caller falls back to the raw LTP."""
    try:
        today = today or datetime.now(IST).date().isoformat()
        candles = market_data.get_candles(instrument_key, '15minute', 3) or []
        closes = [float(c['close']) for c in candles
                  if c.get('close') is not None
                  and str(c.get('timestamp') or '')[:10] == today][-3:]
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


def completed_bars(candles: list, today: str = None) -> list:
    """Drop today's still-forming daily bar (KNOWN_ISSUES P3): at 09:45 it
    holds 30 minutes of trading but weighs like a full day in EMA/momentum/
    consistency, so verdicts drift with run time. Indicators read completed
    structure; the verdict-time PRICE is handled separately (smoothed LTP)."""
    if not candles:
        return candles
    today = today or datetime.now(IST).date().isoformat()
    if str(candles[-1].get('timestamp') or '')[:10] == today:
        return candles[:-1]
    return candles


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

    daily_candles = completed_bars(daily_candles)
    if not daily_candles or len(daily_candles) < MIN_DAILY_BARS:
        return {**base, 'verdict': 'INSUFFICIENT', 'confidence': 0,
                'trend_score': 0, 'reasons':
                [f'Only {len(daily_candles or [])} completed daily bars — '
                 f'need {MIN_DAILY_BARS}+ for an honest read'],
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

    # Higher-timeframe (weekly) structure + how it relates to the daily
    # direction. Surfaced in the reasons and logged, but NOT folded into the
    # numeric score — its weight stays unproven until factor_attribution
    # grades it (dark-flag discipline).
    wk = weekly_trend(daily_candles, price)
    alignment = daily_weekly_alignment(score, wk['weekly_trend'])

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
    if wk['weekly_trend']:
        anchor_wks = min(wk['weekly_weeks'], WEEKLY_EMA_LONG)
        reasons.append(
            f"Weekly trend {wk['weekly_trend'].lower()} — price "
            f"{'above' if (wk['price_vs_weekly_pct'] or 0) >= 0 else 'below'} "
            f"the ~{anchor_wks}-week EMA (higher-timeframe structure)")
        if alignment == 'CONFLICT':
            if score >= 20:
                reasons.append(
                    "⚠ Countertrend: the daily direction is up but the WEEKLY "
                    "trend is down — treat this as a lower-conviction bounce, "
                    "not a durable hold; honor the stop tightly")
            else:
                reasons.append(
                    "Daily weakness sits inside a weekly UPTREND — this may be "
                    "a dip rather than a breakdown; don't reflexively sell "
                    "strength into support")
        elif alignment == 'ALIGNED_DOWN':
            reasons.append(
                "Daily and weekly trends agree (both down) — the exit case is "
                "structural, not a countertrend call")
        elif alignment == 'ALIGNED_UP':
            reasons.append(
                "Daily and weekly trends agree (both up) — higher-conviction hold")
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
            'weekly_trend': wk['weekly_trend'],
            'weekly_ema_long': wk['weekly_ema_long'],
            'price_vs_weekly_pct': wk['price_vs_weekly_pct'],
            'daily_weekly_alignment': alignment,
            'history': history or None,
        },
    }
