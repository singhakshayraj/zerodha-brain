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
                nifty50 = [
                    'NSE:RELIANCE', 'NSE:TCS', 'NSE:HDFCBANK', 'NSE:INFY',
                    'NSE:ICICIBANK', 'NSE:HINDUNILVR', 'NSE:SBIN', 'NSE:BHARTIARTL',
                    'NSE:KOTAKBANK', 'NSE:LT', 'NSE:AXISBANK', 'NSE:BAJFINANCE',
                    'NSE:WIPRO', 'NSE:HCLTECH', 'NSE:MARUTI', 'NSE:SUNPHARMA',
                    'NSE:TITAN', 'NSE:POWERGRID', 'NSE:TATAMOTORS', 'NSE:TATASTEEL',
                    'NSE:JSWSTEEL', 'NSE:HINDALCO', 'NSE:ONGC', 'NSE:COALINDIA',
                    'NSE:BAJAJFINSV', 'NSE:DRREDDY', 'NSE:CIPLA',
                ]
                added = 0
                for sym in nifty50:
                    if sym not in self.universe:
                        parts = sym.split(':', 1)
                        self.universe[sym] = {
                            'symbol': parts[1],
                            'exchange': parts[0],
                            'instrument_token': 0,
                            'source': 'nifty50',
                        }
                        added += 1
                print(f"Added {added} Nifty50 stocks to universe")

            print(f"Universe: {len(self.universe)} stocks (mode: {stock_universe})")

            print(f"Brain initialized. Session: {self.session_id}")
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

            # Fetch prices for Nifty50 stocks not in holdings cache
            nifty_priced = 0
            for sym, data in self.universe.items():
                if data.get('source') == 'nifty50' and sym not in self.market_data._holdings_cache:
                    price = self.market_data.get_live_price_for_nifty50(sym)
                    if price:
                        nifty_priced += 1
            print(f"[brain] Nifty50 prices fetched: {nifty_priced}/27")

            self.traded_symbols_this_cycle = set()

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
                        self._execute_buy(symbol, exchange, live_price, signal)
                        remaining_trades -= 1
                        self.traded_symbols_this_cycle.add(symbol)

                    elif signal['action'] == 'SELL':
                        open_trades = db.get_open_trades(self.session_id)
                        open_symbols = [t['symbol'] for t in open_trades]
                        if symbol in open_symbols:
                            self._execute_sell_by_symbol(
                                symbol, exchange, live_price, signal, 'BRAIN_SIGNAL'
                            )
                            self.traded_symbols_this_cycle.add(symbol)

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

            cycle_time = time.time() - cycle_start_time
            print(
                f"[brain] Cycle {current_cycle} complete in {cycle_time:.1f}s — "
                f"analyzed {analyzed_count} stocks, "
                f"trades: {self.session_stats['trades_executed']}, "
                f"P&L: ₹{self.session_stats['total_pnl']:.2f}"
            )

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

        quantity = self.risk_manager.calculate_position_size(
            capital=capital,
            live_price=live_price,
            confidence=signal['confidence'],
            stop_loss_price=signal['stop_loss'],
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
            self.order_manager.square_off_all(self.kite, open_trades)

        db.end_session(self.session_id, reason)
        db.write_config('brain_status', 'IDLE')
        db.write_config('active_session_id', '')
        print("Session ended.")
