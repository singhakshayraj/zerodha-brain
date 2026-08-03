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
import json
import time
import uuid
from datetime import datetime, timedelta

import pytz

import advisor_backtest
import config
import database as db
import stock_agent
from advisor_risk import (  # noqa: F401  facade re-export (SE4 split)
    CONCENTRATION_FLAG_PCT, SECTOR_CONCENTRATION_PCT,
    portfolio_risk, build_portfolio_risk_lines, _correlation_read,
)
from advisor_digest import (  # noqa: F401  facade re-export (SE4 split)
    build_digest, build_decision_keyboard, send_daily_digest,
)
import market_regime
import news_jobs
import telegram  # noqa: F401  facade re-export (tests patch pa.telegram)
from indicators import run_all_indicators

IST = pytz.timezone('Asia/Kolkata')


# ── Scoring / advise core (SE4 module split part 2, P-06) ────────────────────
# The pure per-holding scoring engine now lives in advisor_scoring; re-exported
# here so callers and tests keep using portfolio_advisor.<name>. Orchestration
# (score_universe / run_advisor* / rotation / timeline capture) stays below.
from advisor_scoring import (  # noqa: F401  facade re-export
    MIN_DAILY_BARS, SWING_LOOKBACK, NEAR_SUPPORT_PCT, OVERSOLD_RSI,
    OVEREXTENDED_RSI, OVEREXTENDED_ABOVE_EMA50_PCT, RELATIVE_STRENGTH_LOOKBACK,
    NIFTY50_INDEX_TOKEN, WEEKLY_EMA_LONG, WEEKLY_EMA_MID, WEEKLY_MOMENTUM_WEEKS,
    trend_consistency, relative_strength, news_sentiment, volume_trend,
    resample_weekly, weekly_trend, daily_weekly_alignment, trend_score,
    classify_trigger, smoothed_last_price, completed_bars, swing_levels,
    breakeven_gain_pct, tradebook_stats, advise,
)


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
            candles = completed_bars(market_data.get_candles(key, 'day', 400))
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
            # Weekly structure of the candidate — the entry-quality gate reads
            # it to refuse rotating INTO a weekly downtrend (FA4/P-09, dark).
            wk = weekly_trend(candles, closes[-1] if closes else None)
            out[sym] = {'symbol': sym, 'score': score,
                        'sector': entry.get('sector'),
                        'last_close': closes[-1] if closes else None,
                        'weekly': wk['weekly_trend']}
        except Exception as e:
            print(f"[advisor.scan] {sym} skipped: {e}")
    if out:
        scored_at = datetime.now(IST).isoformat()
        db.upsert_stock_universe_bulk([
            {'symbol': s['symbol'], 'advisor_score': s['score'],
             'advisor_score_updated_at': scored_at} for s in out.values()])
    return out


from advisor_rotation import (  # noqa: F401  facade re-export
    ROTATION_DEPLOY_FRACTION, find_rotation_candidate, size_rotation,
    rotation_entry_quality, apply_rotation_quality,
)


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


def _capture_stock_timeline(rows: list) -> None:
    """Per-stock agent (mechanical): append each holding's current mechanical
    state to its observation timeline (hourly-deduped so frequent intraday
    refreshes don't flood it), and attach the evolution summary to every row's
    indicators so the verdict reads how the stock has been trending. DARK /
    additive — never changes a verdict. Non-fatal."""
    try:
        sector_of = {u['symbol']: u.get('sector')
                     for u in config.NIFTY500_UNIVERSE}
        phase = stock_agent.observation_phase()
        # Phase-aware dedup. INTRADAY refreshes fire often → hourly dedup so the
        # timeline isn't flooded. PRE_OPEN/POST_CLOSE are once-a-day snapshots →
        # dedup per (phase, today), NOT hourly: otherwise a 15:2X intraday
        # capture hourly-deduped the 15:35 POST_CLOSE into oblivion (P-15).
        if phase == 'INTRADAY':
            cutoff = (datetime.now(IST) - timedelta(hours=1)).isoformat()
            already = db.stock_symbols_observed_since(cutoff)
        else:
            today = datetime.now(IST).date().isoformat()
            already = db.stock_symbols_observed_today_in_phase(today, phase)
        captured = 0
        for row in rows:
            sym = row.get('symbol')
            if not sym:
                continue
            if sym not in already:
                if db.insert_stock_observation(stock_agent.build_observation(
                        sym, row, sector=sector_of.get(sym), phase=phase)):
                    captured += 1
            recent = db.get_recent_observations(sym, limit=24)
            row.setdefault('indicators', {})['timeline_summary'] = \
                stock_agent.summarize_timeline(recent)
        print(f"[advisor.agent] captured {captured}/{len(rows)} stock "
              f"observations (phase {phase}); summaries attached")
    except Exception as e:
        print(f"[advisor] stock-agent capture failed (non-fatal): {e}")


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
        # completed bars only — the benchmark must be trimmed the same way
        # the per-symbol series are, or relative strength compares a full
        # stock day against a partial index day.
        nifty_candles = completed_bars(
            market_data.get_candles('NSE:NIFTY 50', 'day', 400) or [])
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
    closes_by_symbol = {}   # daily closes per name → portfolio_risk v2 correlation
    for h in holdings:
        tsym = h.get('tradingsymbol')
        qty = h.get('quantity') or 0
        if not tsym or qty <= 0:
            continue
        exch = h.get('exchange') or 'NSE'
        try:
            key = f'{exch}:{tsym}'
            candles = market_data.get_candles(key, 'day', 400)
            closes_by_symbol[tsym] = [float(c['close']) for c in (candles or [])
                                      if c.get('close') is not None]
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

                    # Entry-quality read on the target (FA4/P-09, dark) — attach
                    # flags + refuse/resize when enabled; True = rotation refused.
                    if apply_rotation_quality(row, target, sizing, total_value):
                        continue
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

    # Confidence calibration (Pillar 1, DARK): rebuild the reliability curve
    # from the graded track record, store it, and stamp each row with the
    # measured hit-rate for its confidence bucket. Self-refreshing so it stays
    # current without waiting for an on-demand grade_advice run. Logged only —
    # the live `confidence` and verdict are untouched until the curve earns
    # promotion (monotonic on enough calls). Non-fatal.
    try:
        calib_table = advisor_backtest.calibration_curve(
            db.get_evaluated_advice_with_features())
        if calib_table.get('graded_calls'):
            db.write_config('advisor_calibration_latest',
                            json.dumps({**calib_table, 'built_at': run_date}))
            for row in rows:
                cc, low_n = advisor_backtest.calibrated_confidence(
                    row.get('confidence'), calib_table)
                ind = row.setdefault('indicators', {})
                ind['calibrated_confidence'] = cc
                ind['calibration_low_n'] = low_n
            print(f"[advisor.calib] built + dark-attached "
                  f"(n={calib_table['graded_calls']}, "
                  f"ECE {calib_table['ece_pct']}pp, "
                  f"monotonic {calib_table['monotonic']})")
    except Exception as e:
        print(f"[advisor] calibration (dark) failed (non-fatal): {e}")

    # Per-stock agent timeline (mechanical, DARK): capture + summarize.
    _capture_stock_timeline(rows)

    # Portfolio-level risk view (whole book, not per-name): concentration,
    # measured return correlation (v2) with sector clustering fallback,
    # tax-loss-harvest candidates. Non-fatal — a failure here never blocks
    # storing the day's verdicts.
    risk = None
    try:
        sector_map = {u['symbol']: u.get('sector')
                      for u in config.NIFTY500_UNIVERSE}
        risk = portfolio_risk(rows, sector_map=sector_map,
                              closes_by_symbol=closes_by_symbol)
        db.write_config('portfolio_risk_latest',
                        json.dumps({**risk, 'run_date': run_date}))
        corr = risk.get('correlation')
        if corr:
            print(f"[advisor.risk] correlation: {corr['names_covered']} names, "
                  f"effective_bets {corr['effective_bets']} "
                  f"({corr['window_returns']}d window), "
                  f"{len(corr['clusters'])} cluster(s)")
        if risk.get('concentration_flags'):
            for f in risk['concentration_flags']:
                print(f"[advisor.risk] {f}")
        if risk.get('harvestable_loss_inr'):
            print(f"[advisor.risk] tax-loss harvest available: "
                  f"₹{risk['harvestable_loss_inr']:,.0f} across "
                  f"{len(risk['tax_loss_harvest'])} names")
    except Exception as e:
        print(f"[advisor] portfolio risk read failed (non-fatal): {e}")

    run_id = str(uuid.uuid4())
    for row in rows:
        row['is_official'] = True
        row['run_id'] = run_id
    n = db.write_official_portfolio_advice(rows)
    print(f"[advisor] stored {n} recommendations for {run_date} (official, run_id={run_id})")
    send_daily_digest(rows, run_date, risk=risk)   # non-fatal by construction
    return n


def run_advisor_lite(market_data) -> int:
    """Intraday re-score of the holdings (2026-07-14): fresh price/indicators
    only, no Nifty-500 rotation rescan (that's a ~3min/484-name scan, far too
    expensive for a 5-min cadence and unnecessary — rotation targets don't
    meaningfully change within minutes), no digest (would spam Telegram every
    interval), not backtest-eligible (is_official=False). Rotation fields are
    carried forward unchanged from today's official row so the UI still shows
    a 'rotate into X' chip between official runs. ADVISORY ONLY, same as
    run_advisor(). Per-symbol failures skip that symbol, never abort the run."""
    try:
        holdings = market_data.kite.get_holdings() or []
    except Exception as e:
        print(f"[advisor.lite] holdings fetch failed: {e}")
        return 0
    if not holdings:
        return 0

    for h in holdings:
        tsym = h.get('tradingsymbol')
        token = h.get('instrument_token')
        if tsym and token:
            key = f"{h.get('exchange') or 'NSE'}:{tsym}"
            market_data._instrument_cache[key] = token

    history = tradebook_stats(db.get_tradebook())

    nifty_closes = []
    nifty_candles = []
    try:
        market_data._instrument_cache['NSE:NIFTY 50'] = NIFTY50_INDEX_TOKEN
        nifty_candles = completed_bars(
            market_data.get_candles('NSE:NIFTY 50', 'day', 400) or [])
        nifty_closes = [float(c['close']) for c in nifty_candles
                        if c.get('close') is not None]
    except Exception as e:
        print(f"[advisor.lite] nifty benchmark unavailable (non-fatal): {e}")

    regime_info = market_regime.get_market_regime(nifty_candles)
    regime = regime_info['regime']

    total_value = sum(
        (h.get('quantity') or 0) * (h.get('last_price') or 0) for h in holdings
    )

    run_date = datetime.now(IST).date().isoformat()
    today_official = {
        r['symbol']: r for r in db.get_official_advice_for_date(run_date)
    }
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
            # Carry forward today's rotation read rather than rescanning.
            official = today_official.get(tsym)
            if official:
                for k in ('rotation_target_symbol', 'rotation_target_score',
                          'rotation_reason', 'rotation_sell_qty',
                          'rotation_freed_inr', 'rotation_buy_qty',
                          'rotation_buy_price'):
                    if official.get(k) is not None:
                        advice[k] = official[k]
            rows.append({'run_date': run_date, **advice})
        except Exception as e:
            print(f"[advisor.lite] {tsym} failed (skipped): {e}")

    # Per-stock agent timeline (mechanical, DARK): hourly-deduped capture so
    # the intraday cadence feeds the timeline without flooding it.
    _capture_stock_timeline(rows)

    run_id = str(uuid.uuid4())
    for row in rows:
        row['is_official'] = False
        row['run_id'] = run_id
    n = db.insert_portfolio_advice_snapshot(rows)
    print(f"[advisor.lite] stored {n} intraday snapshots for {run_date} "
          f"(run_id={run_id})")
    return n


def run_timeline_capture(market_data) -> int:
    """Always-on per-stock capture (agent P2): a lightweight pass that ONLY
    appends a mechanical observation per holding to the timeline — no rotation
    scan, no digest, no advice-row write. Driven by the scheduler's pre-open
    and post-close slots so the per-stock timeline keeps building outside the
    intraday advisory window, whenever a token is live. Read-only w.r.t.
    orders; hourly-deduped inside _capture_stock_timeline. Per-symbol failures
    skip that symbol."""
    try:
        holdings = market_data.kite.get_holdings() or []
    except Exception as e:
        print(f"[advisor.capture] holdings fetch failed: {e}")
        return 0
    if not holdings:
        return 0

    for h in holdings:
        tsym = h.get('tradingsymbol')
        token = h.get('instrument_token')
        if tsym and token:
            key = f"{h.get('exchange') or 'NSE'}:{tsym}"
            market_data._instrument_cache[key] = token

    history = tradebook_stats(db.get_tradebook())
    nifty_closes, nifty_candles = [], []
    try:
        market_data._instrument_cache['NSE:NIFTY 50'] = NIFTY50_INDEX_TOKEN
        nifty_candles = completed_bars(
            market_data.get_candles('NSE:NIFTY 50', 'day', 400) or [])
        nifty_closes = [float(c['close']) for c in nifty_candles
                        if c.get('close') is not None]
    except Exception as e:
        print(f"[advisor.capture] nifty benchmark unavailable (non-fatal): {e}")
    regime = market_regime.get_market_regime(nifty_candles)['regime']

    rows = []
    for h in holdings:
        tsym = h.get('tradingsymbol')
        qty = h.get('quantity') or 0
        if not tsym or qty <= 0:
            continue
        try:
            key = f"{h.get('exchange') or 'NSE'}:{tsym}"
            candles = market_data.get_candles(key, 'day', 400)
            advice = advise({
                'symbol': tsym, 'quantity': qty,
                'average_price': h.get('average_price'),
                'last_price': h.get('last_price'),
            }, candles or [], history=history.get(tsym),
               nifty_closes=nifty_closes, news_sent=news_sentiment(tsym),
               regime=regime)
            rows.append(advice)
        except Exception as e:
            print(f"[advisor.capture] {tsym} skipped: {e}")

    _capture_stock_timeline(rows)
    print(f"[advisor.capture] timeline pass done: {len(rows)} holdings")
    return len(rows)

