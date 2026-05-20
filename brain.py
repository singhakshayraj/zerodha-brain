import threading
import time
from datetime import datetime

import pytz

import config
import database as db
from kite_client import KiteClient, TokenExpiredError
from market_data import MarketData
from order_manager import OrderManager
from risk_manager import RiskManager
from signal_engine import SignalEngine
from trading_principles import TradingPrinciples

IST = pytz.timezone('Asia/Kolkata')


class TradingBrain:

    def __init__(self):
        self.kite = None
        self.market_data = None
        self.signal_engine = SignalEngine()
        self.risk_manager = RiskManager()
        self.order_manager = OrderManager()
        self.session_config = None
        self.session_id = None
        self.session_stats = {
            'trades_executed': 0,
            'total_pnl': 0.0,
            'winning_trades': 0,
            'losing_trades': 0,
        }
        self.traded_symbols_this_cycle = set()
        self.last_context_log = None
        self.consecutive_losses = 0
        self._nifty50_cache = None
        self._session_ended = False
        self.universe = {}
        self.cycle_count = 0
        self._cycle_lock = threading.Lock()

    def initialize(self, token: str, session_config: dict) -> bool:
        try:
            self.kite = KiteClient(token)
            self.market_data = MarketData(self.kite)
            self.session_config = session_config
            self.session_id = session_config.get('sessionId')

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
            if stock_universe in ('BOTH', 'OPEN_MARKET', 'NIFTY50'):
                added = 0
                for sym, token in config.NIFTY50_INSTRUMENT_TOKENS.items():
                    if sym not in self.universe:
                        parts = sym.split(':', 1)
                        self.universe[sym] = {
                            'symbol': parts[1],
                            'exchange': parts[0],
                            'instrument_token': token,
                            'source': 'nifty50',
                        }
                        if token > 0:
                            self.market_data._instrument_cache[sym] = token
                        added += 1
                print(f"Added {added} Nifty50 stocks to universe")

            print(f"Universe: {len(self.universe)} stocks (mode: {stock_universe})")

            print("[brain] Verifying instrument tokens...")
            bad_tokens = self.market_data.verify_instrument_tokens()
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

            print(f"Brain initialized. Session: {self.session_id}")

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
            limits = self.risk_manager.check_session_limits(
                stats_with_streak, self.session_config
            )
            if not limits['can_trade']:
                print(f"Session limit reached: {limits['reason']}")
                self.end_session(limits['reason'].split(':')[0])
                return

            # Step 3 — filter to stocks we have prices for
            if not self.universe:
                print("No stocks in universe")
                return

            prices_snapshot = dict(self.market_data._holdings_cache)
            price_time = datetime.now(IST)
            print(f"[brain] Price snapshot at {price_time.strftime('%H:%M:%S')} "
                  f"— {len(prices_snapshot)} stocks")

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

            # Step 4
            nifty = self.market_data.get_nifty_level()
            nifty_level = nifty['level'] if nifty else None
            time_bucket = self.risk_manager.get_time_bucket()

            # Step 5
            self._maybe_log_market_context(nifty, time_bucket)

            remaining_trades = (
                self.session_config['maxTrades'] -
                self.session_stats['trades_executed']
            )

            cycle_start_time = time.time()
            analyzed_count = 0

            for key, stock in analyzable.items():
                if remaining_trades <= 0:
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
                    candles_15min = self.market_data.get_candles(
                        key, interval='15minute', days=5
                    )
                    candles_1hour = self.market_data.get_candles(
                        key, interval='60minute', days=20
                    )

                    if not candles_15min:
                        continue

                    quote = prices_snapshot.get(key) or {}
                    live_price = quote.get('price') or quote.get('last_price') or 0
                    if not live_price:
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

                    nifty_change = nifty['change_percent'] if nifty else 0.0
                    nifty_dir = nifty['direction'] if nifty else 'SIDEWAYS'

                    signal = self.signal_engine.generate_signal(
                        candles_5min=candles_5min or [],
                        candles_15min=candles_15min,
                        candles_1hour=candles_1hour or [],
                        live_price=live_price,
                        symbol=symbol,
                        nifty_direction=nifty_dir,
                        nifty_change_percent=nifty_change,
                    )

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

                    db.log_decision(
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
                    )

                    if signal['action'] == 'BUY' and signal['confidence'] >= config.MIN_BUY_CONFIDENCE:
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
                        else:
                            self._execute_buy(symbol, exchange, live_price, signal)
                            remaining_trades -= 1
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
                        ):
                            self._open_short(symbol, exchange, live_price, signal)
                            remaining_trades -= 1
                            self.traded_symbols_this_cycle.add(symbol)
                        else:
                            regime_short = (signal.get('regime') or 'UNK')[:4]
                            self._sell_noops.append(
                                f"{symbol}({signal['confidence']}%{regime_short})"
                            )

                    time.sleep(0.5)

                except Exception as e:
                    print(f"Error analyzing {symbol}: {e}")
                    continue

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
                print("[brain] Cycle 1 complete — verifying Nifty50 tokens with live prices...")
                bad = self.market_data.verify_instrument_tokens()
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
            db.write_config('brain_status', 'TOKEN_EXPIRED')
            self.end_session('TOKEN_EXPIRED')

        except Exception as e:
            print(f"Cycle error: {e}")

        finally:
            self._cycle_lock.release()

    def _execute_buy(self, symbol: str, exchange: str, live_price: float, signal: dict) -> None:
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
        })

        if not trade:
            return

        result = self.order_manager.place_buy_order(
            self.kite, symbol, exchange, quantity
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
            db.update_trade_entry(trade['id'], {
                'entry_order_id': result['order_id'],
                'entry_time': datetime.now(IST).isoformat(),
                'entry_price': result['price'],
                'quantity': result['quantity'],
                'entry_value': result['value'],
            })
            self.session_stats['trades_executed'] += 1
            print(
                f"BUY executed: {symbol} x{result['quantity']} "
                f"@ ₹{result['price']}"
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

    def _open_short(self, symbol: str, exchange: str, live_price: float, signal: dict) -> None:
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
        })
        if not trade:
            return

        result = self.order_manager.place_short_order(
            self.kite, symbol, exchange, quantity
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
            db.update_trade_entry(trade['id'], {
                'entry_order_id': result['order_id'],
                'entry_time': datetime.now(IST).isoformat(),
                'entry_price': result['price'],
                'quantity': result['quantity'],
                'entry_value': result['value'],
            })
            self.session_stats['trades_executed'] += 1
            print(
                f"SHORT opened: {symbol} x{result['quantity']} "
                f"@ ₹{result['price']}"
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
            self.kite, symbol, exchange, qty
        )
        if result:
            entry_value = trade.get('entry_value') or 0
            # For shorts: PnL = entry_value - exit_value (sold high, bought low)
            pnl = entry_value - result['value']
            pnl_pct = (pnl / entry_value) * 100 if entry_value else 0

            db.close_trade(trade['id'], {
                'exit_order_id': result['order_id'],
                'exit_time': datetime.now(IST).isoformat(),
                'exit_price': result['price'],
                'exit_value': result['value'],
                'exit_reason': 'COVER_SHORT',
                'pnl': pnl,
                'pnl_percent': pnl_pct,
            })
            db.update_stock_score(symbol, is_winner=pnl > 0, pnl=pnl)
            self.session_stats['total_pnl'] += pnl
            if pnl > 0:
                self.session_stats['winning_trades'] += 1
            else:
                self.session_stats['losing_trades'] += 1
            db.log_brain_activity(
                session_id=self.session_id,
                activity_type='POSITION_EXIT',
                symbol=symbol,
                message=f"COVER {symbol} — P&L: ₹{pnl:.2f}",
                data={'exit_reason': 'COVER_SHORT', 'pnl': pnl, 'pnl_percent': pnl_pct},
            )

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

    def _check_and_close_positions(self) -> None:
        open_trades = db.get_open_trades(self.session_id)
        if not open_trades:
            return

        symbols = [f"{t['exchange']}:{t['symbol']}" for t in open_trades]
        quotes = self.market_data.get_live_quotes_batch(symbols)

        for trade in open_trades:
            key = f"{trade['exchange']}:{trade['symbol']}"
            quote_data = quotes.get(key, {}) or {}
            current_price = quote_data.get('last_price', 0)

            if not current_price:
                continue

            should_exit = False
            exit_reason = None
            is_short = trade.get('position_type') == 'SHORT'

            if is_short:
                # For shorts: stop ABOVE entry, target BELOW entry
                if current_price >= trade['stop_loss_price']:
                    should_exit = True
                    exit_reason = 'STOP_LOSS_HIT'
                    self.consecutive_losses += 1
                elif current_price <= trade['target_price']:
                    should_exit = True
                    exit_reason = 'TARGET_HIT'
                    self.consecutive_losses = 0
                if should_exit:
                    self._cover_short(trade, current_price)
            else:
                if current_price <= trade['stop_loss_price']:
                    should_exit = True
                    exit_reason = 'STOP_LOSS_HIT'
                    self.consecutive_losses += 1
                elif current_price >= trade['target_price']:
                    should_exit = True
                    exit_reason = 'TARGET_HIT'
                    self.consecutive_losses = 0
                if should_exit:
                    self._execute_sell_by_trade(trade, current_price, exit_reason)

            if self.consecutive_losses >= config.CIRCUIT_BREAKER_CONSECUTIVE_LOSSES:
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
                return

    def _execute_sell_by_trade(self, trade: dict, current_price: float, exit_reason: str) -> None:
        result = self.order_manager.place_sell_order(
            self.kite,
            trade['symbol'],
            trade['exchange'],
            trade['quantity'],
        )

        if result:
            entry_value = trade.get('entry_value') or 0
            pnl = result['value'] - entry_value
            pnl_pct = (pnl / entry_value) * 100 if entry_value else 0

            db.close_trade(trade['id'], {
                'exit_order_id': result['order_id'],
                'exit_time': datetime.now(IST).isoformat(),
                'exit_price': result['price'],
                'exit_value': result['value'],
                'exit_reason': exit_reason,
                'pnl': pnl,
                'pnl_percent': pnl_pct,
            })

            db.update_stock_score(trade['symbol'], is_winner=pnl > 0, pnl=pnl)

            self.session_stats['total_pnl'] += pnl
            if pnl > 0:
                self.session_stats['winning_trades'] += 1
            else:
                self.session_stats['losing_trades'] += 1

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
        trade = next((t for t in open_trades if t['symbol'] == symbol), None)
        if trade:
            self._execute_sell_by_trade(trade, live_price, exit_reason)

    def _maybe_log_market_context(self, nifty, time_bucket: str) -> None:
        now = datetime.now(IST)
        if self.last_context_log:
            elapsed = (now - self.last_context_log).total_seconds()
            if elapsed < config.MARKET_CONTEXT_INTERVAL_SECONDS:
                return

        if nifty:
            vix = 15.0
            db.log_market_context(self.session_id, {
                'session_id': self.session_id,
                'nifty_level': nifty['level'],
                'nifty_change_percent': nifty['change_percent'],
                'nifty_direction': nifty['direction'],
                'india_vix': vix,
                'volatility_bucket': 'LOW' if vix < 13 else 'HIGH' if vix > 20 else 'MEDIUM',
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
                    })

        db.end_session(self.session_id, reason)
        db.write_config('brain_status', 'IDLE')
        db.write_config('active_session_id', '')
        print("Session ended.")
