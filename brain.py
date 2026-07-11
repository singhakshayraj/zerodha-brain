import statistics
import threading
import time
from datetime import datetime

import pytz

import config
import data_jobs
import data_quality
import database as db
import event_calendar
import inplay
import levels
import logger
import orb
import trend_tells
from kite_client import KiteClient, TokenExpiredError
from market_data import MarketData
from order_manager import OrderManager
from paper_broker import PaperBroker
from risk_manager import RiskManager
from signal_engine import SignalEngine
from trading_principles import TradingPrinciples

IST = pytz.timezone('Asia/Kolkata')


def _derive_reason_code(signal: dict) -> str:
    """Machine-readable code for why a decision was NOT an entry
    (ENGINEERING_SPEC REQ-050). Derived from the engine's free-text
    skip_reasons — first match wins, mirroring the engine's own ordering.
    Entries (BUY/SELL) get no code."""
    if signal.get('action') in ('BUY', 'SELL'):
        return None
    for reason in signal.get('skip_reasons') or []:
        r = reason.lower()
        if 'choppy' in r:
            return 'REGIME_CHOPPY'
        if 'regime' in r and 'not suitable' in r:
            return 'REGIME_UNSUITABLE'
        if 'weak_trend' in r:
            return 'WEAK_TREND_CONFIDENCE'
        if 'r:r' in r or 'risk' in r and 'reward' in r:
            return 'RR_BELOW_MIN'
        if 'below thresholds' in r:
            return 'LOW_CONFIDENCE'
        if 'nifty in downtrend' in r or 'nifty in uptrend' in r:
            return 'NIFTY_DIRECTION_BLOCK'
    return 'HOLD_OTHER'


def _dates_in(candles: list) -> list:
    """Ordered unique trade-dates present in a candle list (timestamps look
    like '2026-07-07T09:15:00+0530')."""
    seen = []
    for c in candles or []:
        ts = c.get('timestamp') or ''
        day = ts[:10]
        if day and (not seen or seen[-1] != day):
            seen.append(day)
    return seen


def _compute_trend_tells(signal: dict, candles_5min: list, live_price: float,
                         market_ctx: dict = None) -> dict:
    """Best-effort trend-tell snapshot for logging (REQ-052). Never raises —
    a data gap just means more tells abstain. Non-gating during the paper
    run; the result rides along in the decision row for later validation."""
    try:
        action = signal.get('action')
        direction = 'UP' if action == 'BUY' else 'DOWN' if action == 'SELL' else None
        if direction is None and candles_5min:
            # derive a direction from short-run momentum so HOLD rows still
            # carry a comparable snapshot
            first, last = candles_5min[0]['close'], candles_5min[-1]['close']
            direction = 'UP' if last >= first else 'DOWN'

        days = _dates_in(candles_5min)
        today = days[-1] if days else None
        today_candles = [c for c in (candles_5min or [])
                         if (c.get('timestamp') or '')[:10] == today] if today else []
        prior_candles = [c for c in (candles_5min or [])
                         if (c.get('timestamp') or '')[:10] != today] if today else []

        today_open = today_candles[0]['open'] if today_candles else None
        prev_close = prior_candles[-1]['close'] if prior_candles else None

        # avg prior-session range across the prior dates present
        prior_ranges = []
        for d in days[:-1]:
            day_c = [c for c in candles_5min if (c.get('timestamp') or '')[:10] == d]
            r = trend_tells.session_range(day_c)
            if r is not None:
                prior_ranges.append(r)
        avg_range = sum(prior_ranges) / len(prior_ranges) if prior_ranges else None

        return trend_tells.evaluate(
            direction=direction or 'UP',
            candles_5min=today_candles or candles_5min,
            prev_close=prev_close, today_open=today_open,
            current_price=live_price,
            today_range=trend_tells.session_range(today_candles or candles_5min),
            avg_range=avg_range,
            # breadth now available from universe context; sector still absent
            # so breadth_sector uses breadth-vs-direction agreement as a proxy
            advancers=(market_ctx or {}).get('advancers'),
            decliners=(market_ctx or {}).get('decliners'),
            sector_aligned=(
                None if not market_ctx or market_ctx.get('breadth') is None
                else (market_ctx['direction'] == 'BULLISH' and direction == 'UP')
                     or (market_ctx['direction'] == 'BEARISH' and direction == 'DOWN')
            ),
        )
    except Exception as e:
        print(f"[trend_tells] compute failed (non-fatal): {e}")
        return {}


def _level_context(pack: dict, signal: dict, live_price: float) -> dict:
    """Level snapshot + filter/anchor counterfactual for one decision
    (REQ-020 level snapshot, §5 steps 6–7). Never raises — missing level
    pack just yields an empty snapshot. Returns the snapshot; caller decides
    whether to ACT on it (flag-gated)."""
    try:
        lv = levels.relevant_levels(pack)
        action = signal.get('action')
        direction = 'UP' if action == 'BUY' else 'DOWN' if action == 'SELL' else None
        snap = {'levels_count': len(lv)}
        if not lv or direction is None or not live_price:
            return snap

        atr = (signal.get('indicators') or {}).get('atr_14') or 0
        cur_stop = signal.get('stop_loss') or 0
        r_value = abs(live_price - cur_stop) if cur_stop else None

        filt = levels.level_filter(live_price, direction, r_value, lv)
        anchored = levels.anchored_stop_target(live_price, direction, lv, atr)
        snap['filter'] = filt
        snap['anchored'] = anchored
        return snap
    except Exception as e:
        print(f"[levels] context failed (non-fatal): {e}")
        return {}


def _r_multiple(trade: dict, pnl: float):
    """Signed R-multiple: pnl / (entry-to-stop risk in ₹) — REQ-061. The
    unit every spec metric (expectancy, PF, daily 3R stop) is defined in.
    None when the trade has no usable stop/entry (can't divide by zero)."""
    entry = trade.get('entry_price')
    stop = trade.get('stop_loss_price')
    qty = trade.get('quantity') or 0
    if not entry or not stop or qty <= 0:
        return None  # a None stop must not read as stop=0 (bogus huge risk)
    risk_inr = abs(entry - stop) * qty
    return round(pnl / risk_inr, 3) if risk_inr > 0 else None


class TradingBrain:

    def __init__(self):
        self.kite = None
        self.market_data = None
        self.signal_engine = SignalEngine()
        self.risk_manager = RiskManager()
        # Paper mode swaps ONLY the execution layer — decisions, risk and DB
        # writes run identically against real market data.
        if config.PAPER_TRADING:
            print("[BRAIN] PAPER TRADING mode — no real orders will be placed")
            self.order_manager = PaperBroker()
        else:
            self.order_manager = OrderManager()
        self.session_config = None
        self.session_id = None
        self.config_hash = None
        self.session_stats = {
            'trades_executed': 0,
            'total_pnl': 0.0,
            'winning_trades': 0,
            'losing_trades': 0,
        }
        self.traded_symbols_this_cycle = set()
        self._time_stop_logged = set()  # trade ids with a WOULD_FIRE row
        self._would_stop_logged = set()  # session soft-stop reasons already logged
        self._decision_ts = None        # REQ-073 decision→order clock
        self.last_context_log = None
        self.consecutive_losses = 0
        self._last_loss_exit = {}   # symbol -> datetime of its last losing close
        self._excursion = {}        # trade_id -> {'mfe_r', 'mae_r'} path extremes
        self._nifty50_cache = None
        self._session_ended = False
        self.universe = {}
        self.level_packs = {}   # 'NSE:SYMBOL' -> today's level_pack row
        self.market_ctx = {'level': 0, 'change_percent': 0.0,
                           'direction': 'SIDEWAYS', 'advancers': 0,
                           'decliners': 0, 'breadth': None}
        self.cycle_count = 0
        self._cycle_lock = threading.Lock()

    def resume_stats(self, session_id: str) -> None:
        """Rebuild in-memory session_stats from already-recorded trades.

        A fresh TradingBrain always starts at zero — fine for a genuinely
        new session, but wrong when the scheduler is resuming a RUNNING
        session after a brain restart (crash, redeploy): without this,
        loss/profit limits silently reset to zero and a session already
        near its max-loss threshold gets a free pass on every restart."""
        trades = db.get_session_trades(session_id)
        closed = [t for t in trades if t.get('status') == 'CLOSED']
        executed = [t for t in trades if t.get('entry_price') is not None]
        self.session_stats['trades_executed'] = len(executed)
        self.session_stats['total_pnl'] = sum((t.get('pnl') or 0) for t in closed)
        self.session_stats['winning_trades'] = sum(1 for t in closed if (t.get('pnl') or 0) > 0)
        self.session_stats['losing_trades'] = sum(1 for t in closed if (t.get('pnl') or 0) <= 0)

        # Rebuild the circuit-breaker streak too — resetting it to zero on
        # every restart gave a session with 2 straight losses a free pass.
        # Mirror the live counter's semantics (_record_close_outcome): any
        # losing close extends the streak, any winning close resets it,
        # regardless of exit reason. get_session_trades returns newest-first.
        streak = 0
        for t in closed:
            if (t.get('pnl') or 0) > 0:
                break
            streak += 1
        self.consecutive_losses = streak

        print(
            f"[BRAIN] Resumed stats for session {session_id}: "
            f"{self.session_stats}"
        )

    def initialize(self, token: str, session_config: dict) -> bool:
        try:
            if config.QA_MODE:
                from qa_market import FakeKiteClient
                print("[BRAIN] QA MODE — synthetic market, no Kite calls at all")
                self.kite = FakeKiteClient()
            else:
                self.kite = KiteClient(token)
            self.market_data = MarketData(self.kite)
            self.session_config = session_config
            self.session_id = session_config.get('sessionId')
            self.config_hash = session_config.get('configHash')

            try:
                print("Building instrument map...")
                instruments = self.kite.get_instruments()
                if instruments:
                    print(f"Loaded {len(instruments)} instruments")
            except Exception as e:
                print(f"Instruments fetch skipped: {e}")
                print("Using quote-based token lookup instead")

            try:
                print("Fetching holdings...")
                holdings = self.kite.get_holdings()
                if holdings:
                    db.add_holdings_to_universe(holdings)
                    print(f"Added {len(holdings)} holdings to universe")
                else:
                    print("No holdings found — continuing with Nifty50 only")
            except Exception as e:
                print(f"Holdings fetch failed: {e}")
                print("Continuing without holdings — using Nifty50 universe only")

            self.market_data.clear_cache()

            # Cleanup stale OPEN trades from prior sessions (prevents ghost positions)
            db.cleanup_stale_open_trades(self.session_id)
            # Void unfilled OPEN rows in THIS session (process died between
            # create_trade and the fill) — with quantity NULL they crash
            # _check_and_close_positions every cycle after a resume.
            db.cleanup_unfilled_trades(self.session_id)

            # Propagate session_id to order_manager for safety logging
            self.order_manager.session_id = self.session_id

            # Build universe from holdings (always) + Nifty50 if mode requires
            self.market_data.refresh_holdings_cache()
            self.universe = {}
            for sym, data in self.market_data._holdings_cache.items():
                self.universe[sym] = {
                    'symbol': sym.split(':', 1)[1] if ':' in sym else sym,
                    'exchange': sym.split(':', 1)[0] if ':' in sym else 'NSE',
                    'instrument_token': data.get('instrument_token', 0),
                    'source': 'holdings',
                }
            print(f"Added {len(self.universe)} holdings to universe")

            stock_universe = session_config.get('stockUniverse', 'HOLDINGS')
            # NIFTY50/OPEN_MARKET = top-50 only. BOTH = top-50 + Next-50 —
            # these used to be identical (both just added NIFTY50), which
            # made the universe-breadth experiment axis a no-op.
            token_sources = []
            if stock_universe in ('BOTH', 'OPEN_MARKET', 'NIFTY50'):
                token_sources.append(('nifty50', config.NIFTY50_INSTRUMENT_TOKENS))
            if stock_universe == 'BOTH':
                token_sources.append(('nifty_next50', config.NIFTY_NEXT50_INSTRUMENT_TOKENS))
            for source_label, token_map in token_sources:
                added = 0
                for sym, token in token_map.items():
                    if sym not in self.universe:
                        parts = sym.split(':', 1)
                        self.universe[sym] = {
                            'symbol': parts[1],
                            'exchange': parts[0],
                            'instrument_token': token,
                            'source': source_label,
                        }
                        if token > 0:
                            self.market_data._instrument_cache[sym] = token
                        added += 1
                print(f"Added {added} {source_label} stocks to universe")

            print(f"Universe: {len(self.universe)} stocks (mode: {stock_universe})")

            print("[brain] Verifying instrument tokens...")
            index_map = {
                sym: data['instrument_token']
                for sym, data in self.universe.items()
                if data.get('source') in ('nifty50', 'nifty_next50')
            }
            bad_tokens = self.market_data.verify_instrument_tokens(index_map)
            if bad_tokens:
                print(f"[brain] ⚠️  {len(bad_tokens)} bad tokens detected:")
                for symbol, token, candle_price, cached_price in bad_tokens:
                    print(
                        f"  {symbol}: token={token} "
                        f"candle=₹{candle_price:.2f} "
                        f"cached=₹{cached_price:.2f} → SKIPPING"
                    )
                for symbol, *_ in bad_tokens:
                    if symbol in self.universe:
                        del self.universe[symbol]
                        print(f"[brain] Removed {symbol} from universe")
            else:
                print("[brain] ✅ All instrument tokens verified OK")

            # M3: build today's level pack now — the only moment a valid
            # token is guaranteed (see data_jobs module docstring). Prior-day
            # data, so building post-open loses nothing. Idempotent.
            data_jobs.maybe_build_level_pack(self.market_data, self.universe)
            # Load it back for the level filter / anchored stops (§5 6–7).
            today = datetime.now(IST).strftime('%Y-%m-%d')
            self.level_packs = db.get_level_pack_map(today)
            print(f"[levels] Loaded level packs for {len(self.level_packs)} symbols")

            print(f"Brain initialized. Session: {self.session_id}")

            logger.set_context(
                session_id=self.session_id,
                capital=self.session_config.get('capitalDeployed', 0),
                max_trades=self.session_config.get('maxTrades', 10),
            )
            logger.info(f"Session started: {self.session_id}", tag="session")

            capital_dep = float(self.session_config.get('capitalDeployed') or 0)
            min_capital_needed = 40 / 0.02 / 0.10  # Rs20000
            if capital_dep and capital_dep < min_capital_needed:
                per_trade_pct = 40 / (capital_dep * 0.10) * 100
                print(
                    f"[brokerage] Capital Rs{capital_dep:.0f} is low. "
                    f"Brokerage will be {per_trade_pct:.1f}%+ per trade. "
                    f"Recommend Rs{int(min_capital_needed)}+ for "
                    f"brokerage < 2% per trade."
                )

            win_rate, n_trades = db.get_win_rate()
            if n_trades >= 10:
                print(
                    f"[kelly] ACTIVE: {n_trades} closed trades, "
                    f"win_rate={win_rate:.1%} -> dynamic sizing enabled"
                )
            else:
                print(
                    f"[kelly] INACTIVE: {n_trades}/10 closed trades "
                    f"-> fixed 1% sizing until {10 - n_trades} more trades close"
                )
            return True
        except Exception as e:
            print(f"Brain initialization failed (instrument map): {e}")
            return False

    def run_cycle(self) -> None:
        if not self._cycle_lock.acquire(blocking=False):
            print(f"[brain] Cycle {self.cycle_count + 1} skipped — previous cycle still running")
            return
        try:
            self.cycle_count += 1
            current_cycle = self.cycle_count
            print(f"\n[brain] === Cycle {current_cycle} start: {datetime.now(IST).strftime('%H:%M:%S')} ===")
            logger.set_context(cycle=current_cycle)
            logger.cycle(cycle_num=current_cycle, stocks=len(self.universe))

            # Always fetch fresh prices at cycle start
            print("[brain] Refreshing prices from Zerodha...")
            self.market_data.refresh_holdings_cache()
            print(f"[brain] Prices refreshed for {len(self.market_data._holdings_cache)} stocks")

            # Fetch Nifty50 prices only if not already cached from prior cycle
            nifty_priced = 0
            nifty_total = 0
            for sym, data in self.universe.items():
                if data.get('source') != 'nifty50':
                    continue
                nifty_total += 1
                cached = self.market_data._holdings_cache.get(sym)
                if cached and (cached.get('price') or cached.get('last_price') or 0) > 0:
                    nifty_priced += 1
                    continue
                price = self.market_data.get_live_price_for_nifty50(sym)
                if price:
                    nifty_priced += 1
            print(f"[brain] Nifty50 prices available: {nifty_priced}/{nifty_total}")

            self.traded_symbols_this_cycle = set()
            self._sell_noops = []

            # M3: lock the in-play list at the first cycle past 09:30
            # (idempotent; non-gating — recorded for M4/M5, not enforced).
            data_jobs.maybe_lock_inplay(self.market_data, self.universe)

            # Step 0: EOD cleanup runs FIRST so it fires even if session limit reached
            self._auto_cover_shorts_if_eod()
            self._auto_close_longs_if_eod()
            if self._is_past_ist(15, 25) and not self._session_ended:
                self.end_session('EOD_AUTO')
                return

            # Step 1
            self._check_and_close_positions()
            if self._session_ended:
                return

            # Step 2
            stats_with_streak = dict(self.session_stats)
            stats_with_streak['consecutive_losses'] = self.consecutive_losses
            stats_with_streak['unrealized_pnl'] = self._unrealized_pnl()
            limits = self.risk_manager.check_session_limits(
                stats_with_streak, self.session_config
            )
            if not limits['can_trade']:
                print(f"Session limit reached: {limits['reason']}")
                self.end_session(limits['reason'].split(':')[0])
                return

            # Data-collection mode: a soft limit tripped but we keep trading.
            # Log where a capped run would have ended, once per reason, so the
            # counterfactual is reconstructable from the collected data.
            would_stop = limits.get('would_stop')
            if would_stop:
                marker = would_stop.split(':')[0]
                if marker not in self._would_stop_logged:
                    self._would_stop_logged.add(marker)
                    self._log_activity_safe(
                        'LIMIT_WOULD_STOP', '',
                        f'Counterfactual: {would_stop} — continuing '
                        f'(DATA_COLLECTION_MODE)',
                        {
                            'marker': marker,
                            'reason': would_stop,
                            'trades': self.session_stats.get('trades_executed'),
                            'total_pnl': self.session_stats.get('total_pnl'),
                            'consecutive_losses': self.consecutive_losses,
                        },
                    )

            # Step 3 — filter to stocks we have prices for
            if not self.universe:
                print("No stocks in universe")
                return

            prices_snapshot = dict(self.market_data._holdings_cache)
            price_time = datetime.now(IST)
            print(f"[brain] Price snapshot at {price_time.strftime('%H:%M:%S')} "
                  f"— {len(prices_snapshot)} stocks")

            db.log_quote_snapshot(
                session_id=self.session_id,
                cycle=current_cycle,
                prices={
                    key: (q.get('price') or q.get('last_price') or 0)
                    for key, q in prices_snapshot.items()
                },
            )

            analyzable = {
                key: data for key, data in self.universe.items()
                if key in prices_snapshot
                or self.market_data._instrument_cache.get(key, 0) > 0
            }
            holdings_count = sum(
                1 for d in analyzable.values() if d.get('source') == 'holdings'
            )
            nifty_count = len(analyzable) - holdings_count
            print(f"[brain] Analyzing {len(analyzable)} stocks "
                  f"({holdings_count} holdings, {nifty_count} nifty50)")

            db.log_brain_activity(
                session_id=self.session_id,
                activity_type='CYCLE_START',
                message=f"Cycle {current_cycle} — Scanning {len(analyzable)} stocks "
                        f"({holdings_count} holdings, {nifty_count} nifty50)",
            )

            # Step 4 — real market context from universe breadth (replaces
            # the dead SIDEWAYS stub). Always computed + logged; feeds the
            # signal engine only when MARKET_DIRECTION_ENABLED.
            self.market_ctx = self._market_context()
            nifty = self.market_ctx
            nifty_level = nifty['level'] if nifty else None
            time_bucket = self.risk_manager.get_time_bucket()
            print(f"[market_ctx] dir={nifty['direction']} "
                  f"chg={nifty['change_percent']}% "
                  f"breadth={nifty['advancers']}/{nifty['decliners']} "
                  f"n={nifty.get('sample_size')} rej={nifty.get('rejected')}"
                  f"{' LOWCONF' if nifty.get('low_confidence') else ''} "
                  f"(feed={'ON' if config.MARKET_DIRECTION_ENABLED else 'OFF'})")

            # Step 5
            self._maybe_log_market_context(nifty, time_bucket)

            remaining_trades = (
                self.session_config['maxTrades'] -
                self.session_stats['trades_executed']
            )

            trades_this_cycle = 0
            max_per_cycle = config.MAX_TRADES_PER_CYCLE

            cycle_start_time = time.time()
            analyzed_count = 0
            cycle_candle_rows = []   # buffered OHLCV bars, bulk-upserted below

            for key, stock in analyzable.items():
                if remaining_trades <= 0:
                    break
                if trades_this_cycle >= max_per_cycle:
                    print(
                        f"[brain] Per-cycle limit reached "
                        f"({trades_this_cycle}/{max_per_cycle}) "
                        f"— deferring remaining signals to next cycle"
                    )
                    break

                symbol = stock['symbol']
                exchange = stock.get('exchange', 'NSE')

                if symbol in self.traded_symbols_this_cycle:
                    continue

                tradeable = TradingPrinciples.is_tradeable_indian_stock(symbol)
                if not tradeable['tradeable']:
                    print(f"[{symbol}] {tradeable['reason']}")
                    continue

                try:
                    stock_start = time.time()

                    candles_5min = self.market_data.get_candles(
                        key, interval='5minute', days=3
                    )
                    # Buffer trailing bars for a single bulk upsert at cycle
                    # end — the durable OHLCV archive behind every decision,
                    # for later replay/backtest. One round-trip, not one per
                    # symbol (that added ~7s/cycle, slowing stop detection).
                    if candles_5min:
                        cycle_candle_rows.extend(db.candle_rows(
                            self.session_id, symbol, exchange, candles_5min))
                    candles_15min = self.market_data.get_candles(
                        key, interval='15minute', days=5
                    )
                    candles_1hour = self.market_data.get_candles(
                        key, interval='60minute', days=20
                    )

                    quote = prices_snapshot.get(key) or {}
                    live_price = quote.get('price') or quote.get('last_price') or 0

                    # Data-gap skips are decisions too — log them so the
                    # dataset can tell "brain held" from "brain saw nothing".
                    data_gap = None
                    if not candles_15min:
                        data_gap = 'No 15-minute candle data'
                    elif not live_price:
                        data_gap = 'No live price in quote snapshot'

                    if data_gap:
                        db.log_decision(
                            session_id=self.session_id,
                            symbol=symbol,
                            signal='SKIP',
                            confidence=0,
                            indicators={},
                            reasons=[],
                            skip_reasons=[data_gap],
                            live_price=live_price,
                            nifty_level=nifty_level or 0,
                            time_bucket=time_bucket,
                            reason_code=(
                                'DATA_GAP_CANDLES' if not candles_15min
                                else 'DATA_GAP_PRICE'
                            ),
                            config_hash=self.config_hash,
                            git_sha=config.GIT_SHA,
                        )
                        continue

                    # REQ-050 step 0: quarantine bad data before it can
                    # become a trade. Cross-check the live price against the
                    # latest candle close; a >20% gap is almost always a
                    # wrong token or corrupt quote, not a real move.
                    last_close = (candles_15min[-1]['close']
                                  if candles_15min else None)
                    dq_ok, dq_reason = data_quality.check_quote(
                        live_price, last_candle_close=last_close)
                    if not dq_ok:
                        print(f"[dq] QUARANTINE {symbol}: {dq_reason} "
                              f"(price=₹{live_price} close=₹{last_close})")
                        db.log_decision(
                            session_id=self.session_id, symbol=symbol,
                            signal='SKIP', confidence=0, indicators={},
                            reasons=[], skip_reasons=[dq_reason],
                            live_price=live_price, nifty_level=nifty_level or 0,
                            time_bucket=time_bucket, reason_code=dq_reason,
                            config_hash=self.config_hash, git_sha=config.GIT_SHA,
                        )
                        continue

                    analyzed_count += 1
                    stock_time = time.time() - stock_start
                    if stock_time > 2:
                        print(f"[timing] {symbol} took {stock_time:.1f}s")

                    db.log_brain_activity(
                        session_id=self.session_id,
                        activity_type='ANALYZING',
                        symbol=symbol,
                        message=f"Analyzing {symbol} @ ₹{live_price}",
                    )

                    # Gated feed: when off, pass the neutral values the engine
                    # has always seen (keeps the run comparable); the real
                    # context is still logged on the decision below.
                    if config.MARKET_DIRECTION_ENABLED and nifty:
                        nifty_change = nifty['change_percent']
                        nifty_dir = nifty['direction']
                    else:
                        nifty_change = 0.0
                        nifty_dir = 'SIDEWAYS'

                    # Event-day policy (REQ-053) — computed for logging on
                    # every decision; only gates entries when enabled.
                    event_pol = event_calendar.policy(
                        datetime.now(IST).date(), symbol)

                    signal = self.signal_engine.generate_signal(
                        candles_5min=candles_5min or [],
                        candles_15min=candles_15min,
                        candles_1hour=candles_1hour or [],
                        live_price=live_price,
                        symbol=symbol,
                        nifty_direction=nifty_dir,
                        nifty_change_percent=nifty_change,
                    )
                    # REQ-073: decision→order latency clock starts here.
                    self._decision_ts = time.perf_counter()

                    db.log_brain_activity(
                        session_id=self.session_id,
                        activity_type='SIGNAL',
                        symbol=symbol,
                        message=f"Signal: {signal['action']} — "
                                f"Confidence: {signal['confidence']}% — "
                                f"Regime: {signal.get('regime', 'UNKNOWN')}",
                        data={
                            'action': signal['action'],
                            'confidence': signal['confidence'],
                            'reasons': signal['reasons'],
                            'skip_reasons': signal['skip_reasons'],
                            'rsi': signal['indicators'].get('rsi_14') if signal.get('indicators') else None,
                            'regime': signal.get('regime'),
                            'stop_loss': signal['stop_loss'],
                            'target': signal['target'],
                            'risk_reward': signal['risk_reward_ratio'],
                        },
                    )

                    # Level snapshot + filter/anchor counterfactual (REQ-020,
                    # §5 6–7). Computed for every decision; acted on only when
                    # the flags are enabled (below).
                    level_snap = _level_context(
                        self.level_packs.get(key), signal, live_price)

                    # ORB archetype counterfactual (§5 step 5A). Opening-range
                    # stats from the symbol's own 5-min candles; logged on
                    # every decision, promotes a HOLD only when enabled.
                    or_stats = inplay.opening_range_stats(candles_5min or [])
                    orb_snap = orb.orb_signal(live_price, or_stats)

                    decision_id = db.log_decision(
                        session_id=self.session_id,
                        symbol=symbol,
                        signal=signal['action'],
                        confidence=signal['confidence'],
                        indicators=signal['indicators'],
                        reasons=signal['reasons'],
                        skip_reasons=signal['skip_reasons'],
                        live_price=live_price,
                        nifty_level=nifty_level or 0,
                        time_bucket=time_bucket,
                        stop_loss=signal.get('stop_loss'),
                        target=signal.get('target'),
                        risk_reward=signal.get('risk_reward_ratio'),
                        regime=signal.get('regime', 'UNKNOWN'),
                        market_bias=signal.get('market_bias', 'NEUTRAL'),
                        reason_code=_derive_reason_code(signal),
                        config_hash=self.config_hash,
                        git_sha=config.GIT_SHA,
                        trend_tells=_compute_trend_tells(
                            signal, candles_5min or [], live_price,
                            market_ctx=self.market_ctx),
                        event_policy=event_pol,
                        level_snapshot=level_snap,
                        orb=orb_snap,
                        market_context={
                            'direction': self.market_ctx['direction'],
                            'change_percent': self.market_ctx['change_percent'],
                            'advancers': self.market_ctx['advancers'],
                            'decliners': self.market_ctx['decliners'],
                            'breadth': self.market_ctx['breadth'],
                            'sample_size': self.market_ctx.get('sample_size'),
                            'rejected': self.market_ctx.get('rejected'),
                            'low_confidence': self.market_ctx.get('low_confidence'),
                            'fed_to_engine': config.MARKET_DIRECTION_ENABLED,
                        },
                    )

                    # ORB promotion (§5 step 5A, flag-gated): when the
                    # indicator engine held but a clean opening-range breakout
                    # fired, take the ORB entry. Placed BEFORE the level
                    # override so anchored stops still apply to ORB trades.
                    if (config.ORB_ENABLED
                            and signal['action'] == 'HOLD'
                            and orb_snap['action'] in ('BUY', 'SELL')
                            and orb_snap['confidence'] >= config.ORB_MIN_CONFIDENCE):
                        print(f"[orb] {symbol} promoting HOLD → {orb_snap['action']} "
                              f"(conf {orb_snap['confidence']}, "
                              f"break {orb_snap['break_strength']}× range)")
                        signal['action'] = orb_snap['action']
                        signal['confidence'] = orb_snap['confidence']
                        signal['stop_loss'] = orb_snap['stop_loss']
                        signal['target'] = orb_snap['target']
                        signal['risk_reward_ratio'] = orb_snap['risk_reward_ratio']
                        signal['archetype'] = 'ORB'
                        signal.setdefault('reasons', []).extend(orb_snap['reasons'])
                        db.log_brain_activity(
                            session_id=self.session_id,
                            activity_type='ORB_ENTRY', symbol=symbol,
                            message=f"ORB {orb_snap['action']} @ ₹{live_price} "
                                    f"(OR {orb_snap['or_low']}–{orb_snap['or_high']})",
                            data=orb_snap,
                        )

                    # Level-anchored stops (§5 step 7, flag-gated): replace the
                    # ATR stop/target with structure before sizing + execution.
                    if (config.LEVEL_STOPS_ENABLED
                            and signal['action'] in ('BUY', 'SELL')
                            and level_snap.get('anchored')):
                        a = level_snap['anchored']
                        print(f"[levels] {symbol} anchored stop/target: "
                              f"{signal['stop_loss']}/{signal['target']} → "
                              f"{a['stop']}/{a['target']} (RR {a['rr']})")
                        signal['stop_loss'] = a['stop']
                        signal['target'] = a['target']
                        signal['risk_reward_ratio'] = a['rr']

                    # Level filter (§5 step 6, flag-gated): reject entries with
                    # a wall within block_r × R in the profit direction.
                    if (config.LEVEL_FILTER_ENABLED
                            and signal['action'] in ('BUY', 'SELL')
                            and level_snap.get('filter')
                            and not level_snap['filter']['ok']):
                        print(f"[levels] {symbol} entry blocked by level filter: "
                              f"wall {level_snap['filter']['blocking_level']} "
                              f"@ {level_snap['filter']['distance_r']}R")
                        db.log_brain_activity(
                            session_id=self.session_id,
                            activity_type='LEVEL_BLOCK', symbol=symbol,
                            message=f"level within "
                                    f"{level_snap['filter']['distance_r']}R",
                            data=level_snap['filter'],
                        )
                        continue

                    # Event-day gate (flag-off by default): stand aside on
                    # heavyweights at monthly expiry / results-day symbols;
                    # raise the confidence bar on weekly expiry.
                    if config.EVENT_DAY_ENABLED and signal['action'] in ('BUY', 'SELL'):
                        ep = event_pol['policy']
                        blocked = None
                        if ep == event_calendar.STAND_ASIDE:
                            blocked = 'EVENT_STAND_ASIDE'
                        elif (ep == event_calendar.RAISE_BAR
                              and signal['confidence'] < config.get_tunable('MIN_BUY_CONFIDENCE') + 10):
                            blocked = 'EVENT_RAISE_BAR'
                        if blocked:
                            print(f"[event] {symbol} entry blocked: {blocked} "
                                  f"({event_pol['reasons']})")
                            db.log_brain_activity(
                                session_id=self.session_id,
                                activity_type='EVENT_BLOCK', symbol=symbol,
                                message=f"{blocked}: {event_pol['reasons']}",
                                data=event_pol,
                            )
                            continue

                    ind = signal.get('indicators') or {}
                    logger.signal(
                        symbol=symbol,
                        action=signal['action'],
                        confidence=signal['confidence'],
                        regime=signal.get('regime', 'UNKNOWN'),
                        rsi=ind.get('rsi_14'),
                        atr=ind.get('atr_14'),
                        live_price=live_price,
                        stop_loss=signal.get('stop_loss'),
                        target=signal.get('target'),
                        risk_reward=signal.get('risk_reward_ratio'),
                    )

                    if signal['action'] == 'BUY' and signal['confidence'] >= config.get_tunable('MIN_BUY_CONFIDENCE'):
                        open_trades_now = db.get_open_trades(self.session_id)
                        short_match = next(
                            (t for t in open_trades_now
                             if t['symbol'] == symbol and t.get('position_type') == 'SHORT'),
                            None,
                        )
                        long_match = next(
                            (t for t in open_trades_now
                             if t['symbol'] == symbol and t.get('position_type') != 'SHORT'),
                            None,
                        )
                        if short_match:
                            self._cover_short(short_match, live_price)
                            self.traded_symbols_this_cycle.add(symbol)
                        elif long_match:
                            print(f"[brain] Already long {symbol}, skipping duplicate BUY")
                        elif self._cooldown_gate(symbol):
                            pass   # re-entry cooldown (flag-gated) suppressed it
                        else:
                            self._execute_buy(symbol, exchange, live_price, signal,
                                              decision_id=decision_id)
                            remaining_trades -= 1
                            trades_this_cycle += 1
                            self.traded_symbols_this_cycle.add(symbol)

                    elif signal['action'] == 'SELL':
                        open_trades = db.get_open_trades(self.session_id)
                        open_long_symbols = [
                            t['symbol'] for t in open_trades
                            if t.get('position_type') != 'SHORT'
                        ]
                        open_short_symbols = [
                            t['symbol'] for t in open_trades
                            if t.get('position_type') == 'SHORT'
                        ]
                        is_cnc_holding = any(
                            d.get('source') == 'holdings'
                            for k, d in self.universe.items()
                            if k == key
                        )

                        if symbol in open_long_symbols:
                            # Close existing MIS long
                            self._execute_sell_by_symbol(
                                symbol, exchange, live_price, signal, 'BRAIN_SIGNAL'
                            )
                            self.traded_symbols_this_cycle.add(symbol)
                        elif symbol in open_short_symbols:
                            print(f"[brain] Already short {symbol}, skipping")
                        elif is_cnc_holding:
                            print(f"[SAFETY] Will not short CNC holding: {symbol}")
                        elif (
                            signal.get('regime') == 'TRENDING'
                            and signal['confidence'] >= 65
                        ) or (
                            signal.get('regime') == 'WEAK_TREND'
                            and signal['confidence'] >= 75
                        ) or (
                            # ORB breakdown short (no regime — breakout IS the
                            # thesis); only reachable when ORB_ENABLED promoted it.
                            signal.get('archetype') == 'ORB'
                            and signal['confidence'] >= config.ORB_MIN_CONFIDENCE
                        ):
                            if self._cooldown_gate(symbol):
                                pass   # would short, but re-entry cooldown (flag) suppressed it
                            else:
                                self._open_short(symbol, exchange, live_price, signal,
                                                 decision_id=decision_id)
                                remaining_trades -= 1
                                trades_this_cycle += 1
                                self.traded_symbols_this_cycle.add(symbol)
                        else:
                            regime_short = (signal.get('regime') or 'UNK')[:4]
                            self._sell_noops.append(
                                f"{symbol}({signal['confidence']}%{regime_short})"
                            )

                    time.sleep(0.5)

                except TokenExpiredError:
                    # Must reach the cycle-level handler (below) to end the
                    # session + raise the durable token incident — otherwise
                    # it looks like a per-symbol data gap and stalls silently.
                    raise
                except Exception as e:
                    print(f"Error analyzing {symbol}: {e}")
                    continue

            # One bulk upsert of the cycle's OHLCV bars (non-fatal).
            if cycle_candle_rows:
                db.upsert_candles(cycle_candle_rows)

            db.update_session(self.session_id, {
                'total_trades_executed': self.session_stats['trades_executed'],
                'total_pnl': self.session_stats['total_pnl'],
                'winning_trades': self.session_stats['winning_trades'],
                'losing_trades': self.session_stats['losing_trades'],
            })

            if self._sell_noops:
                print(f"[brain] SELL no-ops ({len(self._sell_noops)}): {', '.join(self._sell_noops)}")

            cycle_time = time.time() - cycle_start_time
            print(
                f"[brain] Cycle {current_cycle} complete in {cycle_time:.1f}s — "
                f"analyzed {analyzed_count} stocks, "
                f"trades: {self.session_stats['trades_executed']}, "
                f"P&L: ₹{self.session_stats['total_pnl']:.2f}"
            )

            if current_cycle == 1:
                print("[brain] Cycle 1 complete — verifying index tokens with live prices...")
                index_map = {
                    sym: data['instrument_token']
                    for sym, data in self.universe.items()
                    if data.get('source') in ('nifty50', 'nifty_next50')
                }
                bad = self.market_data.verify_instrument_tokens(index_map)
                if bad:
                    for symbol, token, candle, cached in bad:
                        print(
                            f"[brain] ⚠️  Removing {symbol} from universe "
                            f"(token={token} candle=₹{candle:.2f} cached=₹{cached:.2f})"
                        )
                        self.universe.pop(symbol, None)
                else:
                    print("[brain] ✅ Nifty50 tokens OK after cycle 1")

        except TokenExpiredError:
            print("Token expired. Stopping session.")
            # Durable incident flag: end_session writes brain_status=IDLE at
            # its end, so a transient TOKEN_EXPIRED status would be gone
            # before the watchdog's next poll. This flag survives for the
            # watchdog to alert on + consume (REQ-071 P1 / REQ-083).
            db.write_config(
                'token_incident',
                f"{datetime.now(IST).isoformat()} token expired mid-session "
                f"(session {self.session_id})",
            )
            db.write_config('brain_status', 'TOKEN_EXPIRED')
            self.end_session('TOKEN_EXPIRED')

        except Exception as e:
            print(f"Cycle error: {e}")
            logger.error(str(e), tag="brain")
            # Feed the error budget: a sustained data/network fault (not a
            # one-off) trips DEGRADED via the heartbeat thread → watchdog
            # alert, instead of dying quietly in the logs (REQ-083).
            db._record_failure()
        else:
            db._record_success()

        finally:
            self._cycle_lock.release()

    def _execute_buy(self, symbol: str, exchange: str, live_price: float, signal: dict,
                     decision_id: str = None) -> None:
        capital = self.session_config['capitalDeployed']

        win_rate, n_trades = db.get_win_rate()
        quantity = self.risk_manager.calculate_position_size(
            capital=capital,
            live_price=live_price,
            confidence=signal['confidence'],
            stop_loss_price=signal['stop_loss'],
            target_price=signal.get('target'),
            historical_win_rate=win_rate,
            n_trades=n_trades,
            symbol=symbol,
        )

        if quantity <= 0:
            print(f"Quantity 0 for {symbol}, skipping")
            return

        trade = db.create_trade(self.session_id, {
            'session_id': self.session_id,
            'symbol': symbol,
            'exchange': exchange,
            'source': 'NIFTY50' if self._is_nifty50(symbol) else 'HOLDINGS',
            'status': 'OPEN',
            'position_type': 'LONG',
            'stop_loss_price': signal['stop_loss'],
            'target_price': signal['target'],
            'risk_reward_ratio': signal['risk_reward_ratio'],
            'regime': signal.get('regime'),
            'confidence_score': signal.get('confidence'),
        })

        if not trade:
            return

        # Link the originating decision's feature vector to this trade's outcome.
        db.link_decision_trade(decision_id, trade.get('id'))

        result = self.order_manager.place_buy_order(
            self.kite, symbol, exchange, quantity,
            **({'hint_price': live_price} if config.PAPER_TRADING else {})
        )

        if result:
            # Sanity check: executed price vs analyzed price
            if live_price and result['price']:
                deviation = abs(result['price'] - live_price) / live_price
                if deviation > 0.05:
                    print(
                        f"[WARNING] BUY price mismatch {symbol}: "
                        f"expected ₹{live_price:.2f} got ₹{result['price']:.2f} "
                        f"({deviation*100:.1f}%) — possible wrong instrument token"
                    )
            entry_payload = {
                'entry_order_id': result['order_id'],
                'entry_time': datetime.now(IST).isoformat(),
                'entry_price': result['price'],
                'quantity': result['quantity'],
                'entry_value': result['value'],
                'decision_to_order_ms': self._decision_latency_ms(),
            }
            exec_entry = self._execution_entry(result)
            if exec_entry:
                entry_payload['execution'] = exec_entry
            db.update_trade_entry(trade['id'], entry_payload)
            self.session_stats['trades_executed'] += 1
            print(
                f"BUY executed: {symbol} x{result['quantity']} "
                f"@ ₹{result['price']}"
            )
            logger.trade(
                symbol=symbol, side='BUY',
                qty=result['quantity'], price=result['price'],
                stop_loss=signal.get('stop_loss'),
                target=signal.get('target'),
                order_id=result['order_id'],
            )
            db.log_brain_activity(
                session_id=self.session_id,
                activity_type='ORDER_PLACED',
                symbol=symbol,
                message=f"BUY {symbol} × {result['quantity']} @ ₹{result['price']}",
                data={
                    'order_id': result['order_id'],
                    'quantity': result['quantity'],
                    'price': result['price'],
                    'value': result['value'],
                },
            )
        else:
            db.close_trade(trade['id'], {
                'exit_reason': 'ORDER_FAILED',
                'pnl': 0,
                'pnl_percent': 0,
            })
            db.log_brain_activity(
                session_id=self.session_id,
                activity_type='ORDER_FAILED',
                symbol=symbol,
                message=f"BUY order failed for {symbol}",
            )

    def _open_short(self, symbol: str, exchange: str, live_price: float, signal: dict,
                    decision_id: str = None) -> None:
        capital = self.session_config['capitalDeployed']
        # Invert stop/target for shorts: signal engine produces long-side levels.
        long_stop = signal['stop_loss']
        long_target = signal['target']
        short_stop = round(live_price + (live_price - long_stop), 2)
        short_target = round(live_price - (long_target - live_price), 2)

        print(
            f"[short_calc] {symbol}: price={live_price:.2f} "
            f"long_stop={long_stop:.2f} → short_stop={short_stop:.2f} "
            f"stop_dist={abs(live_price - short_stop):.2f} "
            f"short_target={short_target:.2f}"
        )

        win_rate, n_trades = db.get_win_rate()
        quantity = self.risk_manager.calculate_position_size(
            capital=capital,
            live_price=live_price,
            confidence=signal['confidence'],
            stop_loss_price=short_stop,
            target_price=short_target,
            historical_win_rate=win_rate,
            n_trades=n_trades,
            symbol=symbol,
        )
        if quantity <= 0:
            print(f"[brain] qty=0 for SHORT {symbol}, skipping")
            return

        trade = db.create_trade(self.session_id, {
            'session_id': self.session_id,
            'symbol': symbol,
            'exchange': exchange,
            'source': 'NIFTY50' if self._is_nifty50(symbol) else 'HOLDINGS',
            'status': 'OPEN',
            'position_type': 'SHORT',
            'stop_loss_price': short_stop,
            'target_price': short_target,
            'risk_reward_ratio': signal['risk_reward_ratio'],
            'regime': signal.get('regime'),
            'confidence_score': signal.get('confidence'),
        })
        if not trade:
            return

        # Link the originating decision's feature vector to this trade's outcome.
        db.link_decision_trade(decision_id, trade.get('id'))

        result = self.order_manager.place_short_order(
            self.kite, symbol, exchange, quantity,
            **({'hint_price': live_price} if config.PAPER_TRADING else {})
        )
        if result:
            if live_price and result['price']:
                deviation = abs(result['price'] - live_price) / live_price
                if deviation > 0.05:
                    print(
                        f"[WARNING] SHORT price mismatch {symbol}: "
                        f"expected ₹{live_price:.2f} got ₹{result['price']:.2f} "
                        f"({deviation*100:.1f}%) — possible wrong instrument token"
                    )
            entry_payload = {
                'entry_order_id': result['order_id'],
                'entry_time': datetime.now(IST).isoformat(),
                'entry_price': result['price'],
                'quantity': result['quantity'],
                'entry_value': result['value'],
                'decision_to_order_ms': self._decision_latency_ms(),
            }
            exec_entry = self._execution_entry(result)
            if exec_entry:
                entry_payload['execution'] = exec_entry
            db.update_trade_entry(trade['id'], entry_payload)
            self.session_stats['trades_executed'] += 1
            print(
                f"SHORT opened: {symbol} x{result['quantity']} "
                f"@ ₹{result['price']}"
            )
            logger.trade(
                symbol=symbol, side='SHORT',
                qty=result['quantity'], price=result['price'],
                stop_loss=short_stop, target=short_target,
                order_id=result['order_id'],
            )
            db.log_brain_activity(
                session_id=self.session_id,
                activity_type='ORDER_PLACED',
                symbol=symbol,
                message=f"SHORT {symbol} × {result['quantity']} @ ₹{result['price']}",
                data={
                    'order_id': result['order_id'],
                    'quantity': result['quantity'],
                    'price': result['price'],
                    'value': result['value'],
                    'position_type': 'SHORT',
                },
            )
        else:
            db.close_trade(trade['id'], {
                'exit_reason': 'ORDER_FAILED',
                'pnl': 0,
                'pnl_percent': 0,
            })

    def _cover_short(self, trade: dict, current_price: float) -> None:
        symbol = trade['symbol']
        exchange = trade.get('exchange', 'NSE')
        qty = trade.get('quantity') or 0
        if qty <= 0:
            return

        result = self.order_manager.cover_short_order(
            self.kite, symbol, exchange, qty,
            **({'hint_price': current_price} if config.PAPER_TRADING else {})
        )
        if result:
            entry_value = trade.get('entry_value') or 0
            # For shorts: PnL = entry_value - exit_value (sold high, bought low)
            pnl = entry_value - result['value']
            pnl_pct = (pnl / entry_value) * 100 if entry_value else 0

            close_payload = {
                'exit_order_id': result['order_id'],
                'exit_time': datetime.now(IST).isoformat(),
                'exit_price': result['price'],
                'exit_value': result['value'],
                'exit_reason': 'COVER_SHORT',
                'pnl': pnl,
                'pnl_percent': pnl_pct,
                'r_multiple': _r_multiple(trade, pnl),
                **self._excursion_fields(trade, result['price']),
            }
            exec_exit = self._execution_exit(trade, result)
            if exec_exit:
                close_payload['execution'] = exec_exit
            db.close_trade(trade['id'], close_payload)
            db.update_stock_score(symbol, is_winner=pnl > 0, pnl=pnl)
            self.session_stats['total_pnl'] += pnl
            if pnl > 0:
                self.session_stats['winning_trades'] += 1
            else:
                self.session_stats['losing_trades'] += 1
            self._record_close_outcome(symbol, pnl)
            db.log_brain_activity(
                session_id=self.session_id,
                activity_type='POSITION_EXIT',
                symbol=symbol,
                message=f"COVER {symbol} — P&L: ₹{pnl:.2f}",
                data={'exit_reason': 'COVER_SHORT', 'pnl': pnl, 'pnl_percent': pnl_pct},
            )

    def _record_close_outcome(self, symbol: str, pnl: float) -> None:
        """Single source of truth for post-close bookkeeping: the
        consecutive-loss streak (drives the circuit breaker) and the
        re-entry cooldown clock. Every realized close routes through here,
        so a losing time-stop or session-end exit counts the same as a clean
        stop — the 2026-07-09 breaker undercount came from only STOP_LOSS_HIT
        touching the counter."""
        if pnl > 0:
            self.consecutive_losses = 0
        else:
            self.consecutive_losses += 1
            self._last_loss_exit[symbol] = datetime.now(IST)

    def _unrealized_r(self, trade: dict, price: float):
        """Signed R-multiple of an OPEN trade at `price`. Same risk unit as
        the realized _r_multiple, so excursions are comparable to outcomes."""
        entry = trade.get('entry_price')
        qty = trade.get('quantity') or 0
        if not entry or qty <= 0 or not price:
            return None
        is_short = trade.get('position_type') == 'SHORT'
        pnl = (entry - price) * qty if is_short else (price - entry) * qty
        return _r_multiple(trade, pnl)

    def _update_excursion(self, trade: dict, price: float) -> None:
        """Track the path extremes of an open trade — Maximum Favorable
        Excursion (best unrealized R reached) and Maximum Adverse Excursion
        (worst). Persisted at close: the signal for whether stops were too
        tight / targets too far, which entry+exit prices alone can't show.
        Called every exit-check cycle and once more at the close price."""
        tid = trade.get('id')
        r = self._unrealized_r(trade, price)
        if tid is None or r is None:
            return
        cur = self._excursion.get(tid)
        if cur is None:
            self._excursion[tid] = {'mfe_r': r, 'mae_r': r}
        else:
            if r > cur['mfe_r']:
                cur['mfe_r'] = r
            if r < cur['mae_r']:
                cur['mae_r'] = r

    def _excursion_fields(self, trade: dict, price: float) -> dict:
        """Final excursion snapshot for the close payload. Folds in the close
        price, then consumes the tracked extremes."""
        self._update_excursion(trade, price)
        exc = self._excursion.pop(trade.get('id'), None)
        if not exc:
            return {}
        return {'mfe_r': exc['mfe_r'], 'mae_r': exc['mae_r']}

    @staticmethod
    def _fill_leg(result: dict) -> dict:
        """One execution leg {reference_price, fill_price, slippage_bps} from a
        broker fill. Only the paper broker decomposes slippage, so the real
        path (no reference_price) yields nothing."""
        return {
            'reference_price': result['reference_price'],
            'fill_price': result['price'],
            'slippage_bps': result.get('slippage_bps'),
        }

    def _execution_entry(self, result: dict):
        """execution jsonb for an entry fill, or None if not decomposed."""
        if not result or result.get('reference_price') is None:
            return None
        return {'entry': self._fill_leg(result)}

    def _execution_exit(self, trade: dict, result: dict):
        """Merge the exit leg into the trade's existing execution block, or
        None if not decomposed. Preserves the entry leg written at open."""
        if not result or result.get('reference_price') is None:
            return None
        block = dict(trade.get('execution') or {})
        block['exit'] = self._fill_leg(result)
        return block

    def _reentry_cooldown(self, symbol: str):
        """(blocked, minutes_since_last_loss). blocked is whether re-entry
        should be suppressed given a recent losing exit on this symbol —
        only meaningful when config.REENTRY_COOLDOWN_ENABLED; callers log the
        counterfactual either way. Returns (False, None) if no prior loss."""
        ts = self._last_loss_exit.get(symbol)
        if not ts:
            return False, None
        mins = (datetime.now(IST) - ts).total_seconds() / 60.0
        return mins < config.REENTRY_COOLDOWN_MIN, mins

    def _cooldown_gate(self, symbol: str) -> bool:
        """Entry guard. Returns True if the caller should SKIP this entry.
        Always logs the counterfactual; only actually blocks when the flag is
        on (dark feature — measure the effect before enabling)."""
        blocked, mins = self._reentry_cooldown(symbol)
        if not blocked:
            return False
        if config.REENTRY_COOLDOWN_ENABLED:
            print(f"[cooldown] skip {symbol} — losing exit {mins:.0f}m ago "
                  f"(< {config.REENTRY_COOLDOWN_MIN}m)")
            self._log_activity_safe(
                'REENTRY_BLOCKED', symbol,
                f"cooldown: {symbol} lost {mins:.0f}m ago (< "
                f"{config.REENTRY_COOLDOWN_MIN}m) — entry blocked",
                {'minutes_since_loss': round(mins, 1),
                 'cooldown_min': config.REENTRY_COOLDOWN_MIN})
            return True
        # flag off — record what it WOULD have blocked, then allow
        self._log_activity_safe(
            'REENTRY_WOULD_BLOCK', symbol,
            f"cooldown would block {symbol} (lost {mins:.0f}m ago) — disabled",
            {'minutes_since_loss': round(mins, 1),
             'cooldown_min': config.REENTRY_COOLDOWN_MIN})
        return False

    def _log_activity_safe(self, activity_type: str, symbol: str,
                           message: str, data: dict) -> None:
        try:
            db.log_brain_activity(session_id=self.session_id,
                                  activity_type=activity_type,
                                  symbol=symbol, message=message, data=data)
        except Exception:
            pass

    def _decision_latency_ms(self):
        """ms from the entry decision to this order (REQ-073). None if no
        clock was started (e.g. exits, which don't originate from a cycle
        decision)."""
        if self._decision_ts is None:
            return None
        return round((time.perf_counter() - self._decision_ts) * 1000, 1)

    def _market_context(self) -> dict:
        """Real market direction + breadth from the universe itself: each
        stock's day change = (live − prior-day close) computed from the
        level pack's PDC and the live price. Retail enctoken can't read the
        Nifty index (/quote disabled), so get_nifty_level was stubbed to a
        constant SIDEWAYS — the dead input behind 2026-07-08's shorts into a
        rising tape. This reconstructs the same signal from data we already
        have, and yields breadth for the trend-tells breadth_sector tell.

        Returns {level, change_percent, direction, advancers, decliners,
        breadth, sample_size, rejected, low_confidence}. Never raises.

        Per-stock moves beyond config.MARKET_MAX_STOCK_MOVE_PCT are dropped as
        bad data (garbage PDC / wrong token / unadjusted split), and a result
        built on fewer than config.MARKET_BREADTH_MIN_SAMPLES clean stocks is
        flagged low_confidence and forced SIDEWAYS — both guard the audit
        block from the 2026-07-09 case (change=123.9% off two bad packs)."""
        max_move = config.MARKET_MAX_STOCK_MOVE_PCT
        try:
            changes = []
            advancers = decliners = rejected = 0
            for key, data in self.universe.items():
                pack = self.level_packs.get(key)
                pdc = (pack or {}).get('pdc')
                quote = self.market_data._holdings_cache.get(key, {}) or {}
                live = quote.get('price') or quote.get('last_price') or 0
                if not pdc or float(pdc) <= 0 or not live:
                    continue
                pct = (live - float(pdc)) / float(pdc) * 100
                if abs(pct) > max_move:
                    rejected += 1          # implausible move → bad reference data
                    continue
                changes.append(pct)
                if pct > 0:
                    advancers += 1
                elif pct < 0:
                    decliners += 1
            sample_size = len(changes)
            avg = (sum(changes) / sample_size) if sample_size else 0.0
            # Realized-volatility proxy: cross-sectional stdev (%) of the
            # universe's day-change distribution. Retail enctoken can't read
            # the VIX index, but the dispersion of our own universe's moves is
            # a real, live volatility signal (replaces the dead india_vix=15).
            realized_vol = (round(statistics.pstdev(changes), 3)
                            if sample_size >= 2 else None)
            low_confidence = sample_size < config.MARKET_BREADTH_MIN_SAMPLES
            if low_confidence:
                # Not enough clean breadth to make a directional call.
                return {'level': 0, 'change_percent': round(avg, 3),
                        'direction': 'SIDEWAYS', 'advancers': advancers,
                        'decliners': decliners, 'breadth': None,
                        'realized_vol': realized_vol,
                        'sample_size': sample_size, 'rejected': rejected,
                        'low_confidence': True}
            direction = ('BULLISH' if avg >= 0.5 else
                         'BEARISH' if avg <= -0.5 else 'SIDEWAYS')
            breadth = advancers / (advancers + decliners) if (advancers + decliners) else None
            return {'level': 0, 'change_percent': round(avg, 3),
                    'direction': direction, 'advancers': advancers,
                    'decliners': decliners,
                    'breadth': round(breadth, 3) if breadth is not None else None,
                    'realized_vol': realized_vol,
                    'sample_size': sample_size, 'rejected': rejected,
                    'low_confidence': False}
        except Exception as e:
            print(f"[market_ctx] failed (non-fatal): {e}")
            return {'level': 0, 'change_percent': 0.0, 'direction': 'SIDEWAYS',
                    'advancers': 0, 'decliners': 0, 'breadth': None,
                    'realized_vol': None,
                    'sample_size': 0, 'rejected': 0, 'low_confidence': True}

    def _unrealized_pnl(self) -> float:
        """Mark-to-market P&L of open positions from the cycle's price
        cache. session_stats['total_pnl'] is realized-only, so an open
        position bleeding past the max-loss limit went undetected until its
        own stop closed it."""
        total = 0.0
        try:
            for t in db.get_open_trades(self.session_id):
                entry_value = t.get('entry_value') or 0
                qty = t.get('quantity') or 0
                if not entry_value or qty <= 0:
                    continue
                key = f"{t.get('exchange', 'NSE')}:{t['symbol']}"
                quote = self.market_data._holdings_cache.get(key, {}) or {}
                price = quote.get('price') or quote.get('last_price') or 0
                if not price:
                    continue
                current_value = price * qty
                if t.get('position_type') == 'SHORT':
                    total += entry_value - current_value
                else:
                    total += current_value - entry_value
        except Exception as e:
            print(f"[brain] _unrealized_pnl error (treating as 0): {e}")
            return 0.0
        return total

    def _minutes_open(self, trade: dict):
        """Minutes since entry, from entry_time. None if unparseable (an
        unfilled/corrupt row must not read as age 0 and trigger nothing, nor
        as huge and trigger a spurious exit)."""
        raw = trade.get('entry_time')
        if not raw:
            return None
        try:
            entry = datetime.fromisoformat(str(raw))
            if entry.tzinfo is None:
                entry = IST.localize(entry)
            return (datetime.now(IST) - entry).total_seconds() / 60.0
        except Exception:
            return None

    def _is_past_ist(self, hour: int, minute: int) -> bool:
        now = datetime.now(IST)
        return (now.hour, now.minute) >= (hour, minute)

    def _auto_cover_shorts_if_eod(self) -> None:
        """Cover all open shorts at/after 3:15 PM IST."""
        if not self._is_past_ist(15, 15):
            return
        open_shorts = db.get_open_shorts(self.session_id)
        if not open_shorts:
            return
        print(f"[eod] 15:15 IST — covering {len(open_shorts)} open shorts")
        for s in open_shorts:
            key = f"{s.get('exchange', 'NSE')}:{s['symbol']}"
            quote = self.market_data._holdings_cache.get(key, {}) or {}
            price = quote.get('price') or quote.get('last_price') or 0
            if not price:
                price = self.market_data.get_live_price_for_nifty50(key) or 0
            try:
                self._cover_short(s, price)
            except Exception as e:
                print(f"[eod] Failed to cover {s.get('symbol')}: {e}")

    def _auto_close_longs_if_eod(self) -> None:
        """Close all open longs at/after 3:20 PM IST."""
        if not self._is_past_ist(15, 20):
            return
        open_longs = db.get_open_longs(self.session_id)
        if not open_longs:
            return
        print(f"[eod] 15:20 IST — closing {len(open_longs)} open longs")
        for t in open_longs:
            key = f"{t.get('exchange', 'NSE')}:{t['symbol']}"
            quote = self.market_data._holdings_cache.get(key, {}) or {}
            price = quote.get('price') or quote.get('last_price') or 0
            if not price:
                price = self.market_data.get_live_price_for_nifty50(key) or 0
            try:
                self._execute_sell_by_trade(t, price, 'EOD_CLOSE')
            except Exception as e:
                print(f"[eod] Failed to close {t.get('symbol')}: {e}")

    def _exit_price_for(self, trade: dict):
        """Freshest price available for an exit decision: TTL-bypassing
        candle close first (the cached quote is up to a full cycle stale —
        the −2.78R stop fills of 2026-07-08), holdings cache as fallback."""
        key = f"{trade.get('exchange', 'NSE')}:{trade['symbol']}"
        price = self.market_data.get_fresh_close(key)
        if price:
            return price
        quote = self.market_data._holdings_cache.get(key, {}) or {}
        return quote.get('price') or quote.get('last_price') or 0

    def _evaluate_exit(self, trade: dict, current_price: float) -> bool:
        """Stop → target → time-stop for ONE open trade, priority order
        (REQ-051). Returns True if the position was exited. May end the
        session via the circuit breaker."""
        should_exit = False
        exit_reason = None
        is_short = trade.get('position_type') == 'SHORT'

        # Track path extremes every cycle (MFE/MAE), including this one — the
        # close funcs fold in the final price and persist them.
        self._update_excursion(trade, current_price)

        # The consecutive-loss streak is NOT updated here — it's owned by
        # _record_close_outcome, called from every close path by realized
        # pnl sign. Counting it here (only on STOP_LOSS_HIT) is what let the
        # circuit breaker undercount a losing streak on 2026-07-09.
        if is_short:
            # For shorts: stop ABOVE entry, target BELOW entry
            if current_price >= trade['stop_loss_price']:
                should_exit, exit_reason = True, 'STOP_LOSS_HIT'
            elif current_price <= trade['target_price']:
                should_exit, exit_reason = True, 'TARGET_HIT'
            if should_exit:
                self._cover_short(trade, current_price)
        else:
            if current_price <= trade['stop_loss_price']:
                should_exit, exit_reason = True, 'STOP_LOSS_HIT'
            elif current_price >= trade['target_price']:
                should_exit, exit_reason = True, 'TARGET_HIT'
            if should_exit:
                self._execute_sell_by_trade(trade, current_price, exit_reason)

        # Time-stop (REQ-051): only if stop/target did NOT fire, preserving
        # the stop→target→time-stop priority. Flag-gated.
        if not should_exit:
            mins = self._minutes_open(trade)
            limit = (config.TIME_STOP_MIN_SHORT if is_short
                     else config.TIME_STOP_MIN)
            if mins is not None and mins >= limit:
                if config.TIME_STOP_ENABLED:
                    print(f"[time_stop] {trade['symbol']} open {mins:.0f}m "
                          f">= {limit}m — cutting (dead money)")
                    if is_short:
                        self._cover_short(trade, current_price)
                    else:
                        self._execute_sell_by_trade(trade, current_price, 'TIME_STOP')
                    should_exit = True
                elif trade['id'] not in self._time_stop_logged:
                    # Would-fire counterfactual, once per trade — exit checks
                    # now run every ~30s and this would spam otherwise.
                    self._time_stop_logged.add(trade['id'])
                    db.log_brain_activity(
                        session_id=self.session_id,
                        activity_type='TIME_STOP_WOULD_FIRE',
                        symbol=trade['symbol'],
                        message=f"time-stop would cut {trade['symbol']} "
                                f"({mins:.0f}m >= {limit}m) — disabled",
                        data={'minutes_open': round(mins, 1), 'limit': limit},
                    )

        if self.consecutive_losses >= config.CIRCUIT_BREAKER_CONSECUTIVE_LOSSES:
            if config.data_collection_active():
                # Counterfactual: log where the breaker would have fired, once,
                # but keep trading to collect the full day.
                if 'CIRCUIT_BREAKER' not in self._would_stop_logged:
                    self._would_stop_logged.add('CIRCUIT_BREAKER')
                    self._log_activity_safe(
                        'LIMIT_WOULD_STOP', '',
                        f'Counterfactual: CIRCUIT_BREAKER '
                        f'({self.consecutive_losses} consecutive losses) — '
                        f'continuing (DATA_COLLECTION_MODE)',
                        {
                            'marker': 'CIRCUIT_BREAKER',
                            'consecutive_losses': self.consecutive_losses,
                            'trades': self.session_stats.get('trades_executed'),
                            'total_pnl': self.session_stats.get('total_pnl'),
                        },
                    )
            else:
                print(
                    "CIRCUIT BREAKER: 3 consecutive losses. "
                    "Stopping session to protect capital."
                )
                db.update_heartbeat(
                    'RUNNING',
                    self.session_stats['trades_executed'],
                    'CIRCUIT BREAKER: 3 consecutive losses — stopping',
                )
                self.end_session('CIRCUIT_BREAKER')
        return should_exit

    def _check_and_close_positions(self) -> None:
        open_trades = db.get_open_trades(self.session_id)
        if not open_trades:
            return
        for trade in open_trades:
            current_price = self._exit_price_for(trade)
            if not current_price:
                continue
            self._evaluate_exit(trade, current_price)
            if self._session_ended:
                return

    def check_open_exits(self) -> None:
        """Intra-cycle stop/target enforcement — called from the scheduler's
        10s slice loop (~every 30s). The 2026-07-08 session showed stops
        detected only at cycle boundaries fill at −2.78R instead of ≈−1R;
        this closes that latency gap. Never raises — token expiry ends the
        session exactly like the cycle handler does."""
        if self._session_ended:
            return
        # skip if a cycle is mid-flight (single-threaded scheduler makes
        # this rare; belt-and-suspenders for future concurrency)
        if not self._cycle_lock.acquire(blocking=False):
            return
        try:
            self._check_and_close_positions()
        except TokenExpiredError:
            print("Token expired (intra-cycle exit check). Stopping session.")
            db.write_config(
                'token_incident',
                f"{datetime.now(IST).isoformat()} token expired mid-session "
                f"(session {self.session_id})",
            )
            db.write_config('brain_status', 'TOKEN_EXPIRED')
            self.end_session('TOKEN_EXPIRED')
        except Exception as e:
            print(f"[brain] intra-cycle exit check failed (non-fatal): {e}")
        finally:
            self._cycle_lock.release()

    def _execute_sell_by_trade(self, trade: dict, current_price: float, exit_reason: str) -> None:
        qty = trade.get('quantity') or 0
        if qty <= 0:
            # Unfilled/corrupt row — a None quantity here used to TypeError
            # inside the broker and abort the whole cycle.
            print(f"[brain] Skipping exit for {trade.get('symbol')}: quantity={trade.get('quantity')!r}")
            return
        result = self.order_manager.place_sell_order(
            self.kite,
            trade['symbol'],
            trade['exchange'],
            qty,
            **({'hint_price': current_price} if config.PAPER_TRADING else {})
        )

        if result:
            entry_value = trade.get('entry_value') or 0
            pnl = result['value'] - entry_value
            pnl_pct = (pnl / entry_value) * 100 if entry_value else 0

            close_payload = {
                'exit_order_id': result['order_id'],
                'exit_time': datetime.now(IST).isoformat(),
                'exit_price': result['price'],
                'exit_value': result['value'],
                'exit_reason': exit_reason,
                'pnl': pnl,
                'pnl_percent': pnl_pct,
                'r_multiple': _r_multiple(trade, pnl),
                **self._excursion_fields(trade, result['price']),
            }
            exec_exit = self._execution_exit(trade, result)
            if exec_exit:
                close_payload['execution'] = exec_exit
            db.close_trade(trade['id'], close_payload)

            db.update_stock_score(trade['symbol'], is_winner=pnl > 0, pnl=pnl)

            self.session_stats['total_pnl'] += pnl
            if pnl > 0:
                self.session_stats['winning_trades'] += 1
            else:
                self.session_stats['losing_trades'] += 1
            self._record_close_outcome(trade['symbol'], pnl)

            db.log_brain_activity(
                session_id=self.session_id,
                activity_type='POSITION_EXIT',
                symbol=trade['symbol'],
                message=f"EXIT {trade['symbol']} — {exit_reason} — "
                        f"P&L: ₹{pnl:.2f}",
                data={
                    'exit_reason': exit_reason,
                    'pnl': pnl,
                    'pnl_percent': pnl_pct,
                },
            )

    def _execute_sell_by_symbol(
        self,
        symbol: str,
        exchange: str,
        live_price: float,
        signal: dict,
        exit_reason: str,
    ) -> None:
        open_trades = db.get_open_trades(self.session_id)
        # LONG rows only — matching by symbol alone could grab a SHORT row
        # and "sell" it again if long+short ever coexist (e.g. two instances).
        trade = next(
            (t for t in open_trades
             if t['symbol'] == symbol and t.get('position_type') != 'SHORT'),
            None,
        )
        if trade:
            self._execute_sell_by_trade(trade, live_price, exit_reason)

    def _maybe_log_market_context(self, nifty, time_bucket: str) -> None:
        now = datetime.now(IST)
        if self.last_context_log:
            elapsed = (now - self.last_context_log).total_seconds()
            if elapsed < config.MARKET_CONTEXT_INTERVAL_SECONDS:
                return

        if nifty:
            # Realized-vol proxy from the universe (cross-sectional day-change
            # stdev). Bucket on the dispersion, not the dead india_vix=15
            # constant. Typical large-cap intraday dispersion ~0.5–1.5%.
            rvol = nifty.get('realized_vol')
            if rvol is None:
                vol_bucket = 'UNKNOWN'
            elif rvol < 0.7:
                vol_bucket = 'LOW'
            elif rvol > 1.5:
                vol_bucket = 'HIGH'
            else:
                vol_bucket = 'MEDIUM'
            db.log_market_context(self.session_id, {
                'session_id': self.session_id,
                'nifty_level': nifty['level'],
                'nifty_change_percent': nifty['change_percent'],
                'nifty_direction': nifty['direction'],
                'india_vix': None,  # deprecated: retail token can't read VIX
                'realized_vol': rvol,
                'volatility_bucket': vol_bucket,
                'time_bucket': time_bucket,
            })
            self.last_context_log = now

    def _is_nifty50(self, symbol: str) -> bool:
        if self._nifty50_cache is None:
            universe = db.get_stock_universe('NIFTY50')
            self._nifty50_cache = {s['symbol'] for s in universe}
        return symbol in self._nifty50_cache

    def end_session(self, reason: str) -> None:
        print(f"Ending session. Reason: {reason}")
        self._session_ended = True
        try:
            db.log_brain_activity(
                session_id=self.session_id,
                activity_type='SESSION_END',
                message=f"Session ended: {reason}",
                data={
                    'reason': reason,
                    'total_pnl': self.session_stats['total_pnl'],
                    'trades_executed': self.session_stats['trades_executed'],
                    'winning_trades': self.session_stats['winning_trades'],
                    'losing_trades': self.session_stats['losing_trades'],
                },
            )
        except Exception:
            pass

        open_trades = db.get_open_trades(self.session_id)
        if open_trades:
            print(f"Squaring off {len(open_trades)} positions...")
            for t in open_trades:
                key = f"{t.get('exchange', 'NSE')}:{t['symbol']}"
                quote = self.market_data._holdings_cache.get(key, {}) or {}
                price = quote.get('price') or quote.get('last_price') or 0
                if not price:
                    price = self.market_data.get_live_price_for_nifty50(key) or 0

                trade_still_open = True
                try:
                    if t.get('position_type') == 'SHORT':
                        print(f"[square_off] Covering short: {t['symbol']} x{t.get('quantity', 0)}")
                        self._cover_short(t, price)
                    else:
                        print(f"[square_off] Closing long: {t['symbol']} x{t.get('quantity', 0)}")
                        self._execute_sell_by_trade(t, price, 'SESSION_END')

                    # Verify trade was actually closed (order may have failed)
                    fresh = [r for r in db.get_open_trades(self.session_id) if r['id'] == t['id']]
                    trade_still_open = bool(fresh)
                except Exception as e:
                    print(f"[square_off] Error closing {t['symbol']}: {e}")

                # Force-close in DB if Kite order failed — prevents stale OPEN trades
                if trade_still_open:
                    entry_val = t.get('entry_value') or 0
                    is_short = t.get('position_type') == 'SHORT'
                    exit_val = (price or 0) * (t.get('quantity') or 0)
                    pnl = (entry_val - exit_val) if is_short else (exit_val - entry_val)
                    pnl_pct = (pnl / entry_val * 100) if entry_val else 0
                    print(f"[square_off] Force-closing {t['symbol']} in DB (order failed)")
                    db.close_trade(t['id'], {
                        'exit_time': datetime.now(IST).isoformat(),
                        'exit_price': price,
                        'exit_value': exit_val,
                        'exit_reason': 'SQUARE_OFF_FAILED',
                        'pnl': pnl,
                        'pnl_percent': pnl_pct,
                        'r_multiple': _r_multiple(t, pnl),
                    })

        logger.info(
            f"Session ended: {reason} trades={self.session_stats['trades_executed']} "
            f"pnl=Rs{self.session_stats['total_pnl']:.2f}",
            tag="session",
            reason=reason,
            trades=self.session_stats['trades_executed'],
            pnl=self.session_stats['total_pnl'],
            winning_trades=self.session_stats['winning_trades'],
            losing_trades=self.session_stats['losing_trades'],
        )
        logger.clear_context()
        db.end_session(self.session_id, reason)
        db.write_config('brain_status', 'IDLE')
        db.write_config('active_session_id', '')
        print("Session ended.")
