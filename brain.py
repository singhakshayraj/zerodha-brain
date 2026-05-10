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

    def initialize(self, token: str, session_config: dict) -> bool:
        try:
            self.kite = KiteClient(token)
            self.market_data = MarketData(self.kite)
            self.session_config = session_config
            self.session_id = session_config.get('sessionId')

            print("Building instrument map...")
            self.kite.get_instruments()

            print("Fetching holdings...")
            holdings = self.kite.get_holdings()
            if holdings:
                db.add_holdings_to_universe(holdings)

            self.market_data.clear_cache()
            print(f"Brain initialized. Session: {self.session_id}")
            return True
        except Exception as e:
            print(f"Brain initialization failed: {e}")
            return False

    def run_cycle(self) -> None:
        try:
            print(f"\n--- Cycle start: {datetime.now(IST).strftime('%H:%M:%S')} ---")

            self.traded_symbols_this_cycle = set()

            # Step 1
            self._check_and_close_positions()

            # Step 2
            limits = self.risk_manager.check_session_limits(
                self.session_stats, self.session_config
            )
            if not limits['can_trade']:
                print(f"Session limit reached: {limits['reason']}")
                self.end_session(limits['reason'].split(':')[0])
                return

            # Step 3
            universe = db.get_stock_universe('ALL')
            if not universe:
                print("No stocks in universe")
                return

            # Step 4
            nifty = self.market_data.get_nifty_level()
            nifty_level = nifty['level'] if nifty else None
            time_bucket = self.risk_manager.get_time_bucket()

            # Step 5
            self._maybe_log_market_context(nifty, time_bucket)

            # Step 6
            remaining_trades = (
                self.session_config['maxTrades'] -
                self.session_stats['trades_executed']
            )

            for stock in universe:
                if remaining_trades <= 0:
                    break

                symbol = stock['symbol']
                exchange = stock.get('exchange', 'NSE')

                if symbol in self.traded_symbols_this_cycle:
                    continue

                try:
                    candles = self.market_data.get_candles(
                        f"{exchange}:{symbol}", interval='15minute', days=5
                    )
                    quote = self.market_data.get_live_quote(f"{exchange}:{symbol}")

                    if not candles or not quote:
                        continue

                    live_price = quote.get('last_price', 0) if isinstance(quote, dict) else 0
                    if not live_price:
                        continue

                    signal = self.signal_engine.generate_signal(
                        candles, live_price, symbol
                    )

                    db.log_decision(self.session_id, {
                        'session_id': self.session_id,
                        'trade_id': None,
                        'symbol': symbol,
                        'price_at_decision': live_price,
                        'nifty_level_at_decision': nifty_level,
                        'time_of_day_bucket': time_bucket,
                        'indicators': signal['indicators'],
                        'signal': signal['action'],
                        'confidence_score': signal['confidence'],
                        'reasons': signal['reasons'],
                        'skip_reasons': signal['skip_reasons'],
                    })

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

            print(
                f"Cycle complete. Trades: {self.session_stats['trades_executed']}, "
                f"P&L: ₹{self.session_stats['total_pnl']:.2f}"
            )

        except TokenExpiredError:
            print("Token expired. Stopping session.")
            db.write_config('brain_status', 'TOKEN_EXPIRED')
            self.end_session('TOKEN_EXPIRED')

        except Exception as e:
            print(f"Cycle error: {e}")

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
        else:
            db.close_trade(trade['id'], {
                'exit_reason': 'ORDER_FAILED',
                'pnl': 0,
                'pnl_percent': 0,
            })

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

            if self.consecutive_losses >= 3:
                print("WARNING: 3 consecutive losses. Consider stopping session.")
                db.update_heartbeat(
                    'RUNNING',
                    self.session_stats['trades_executed'],
                    'WARNING: 3 consecutive losses',
                )

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

        open_trades = db.get_open_trades(self.session_id)
        if open_trades:
            print(f"Squaring off {len(open_trades)} positions...")
            self.order_manager.square_off_all(self.kite, open_trades)

        db.end_session(self.session_id, reason)
        db.write_config('brain_status', 'IDLE')
        db.write_config('active_session_id', '')
        print("Session ended.")
