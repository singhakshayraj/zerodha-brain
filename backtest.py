"""Gate #6 backtest harness (VISION.md §5 gate 6, §6.1).

Replays the deterministic pipeline — in-play filter (§2.1) -> §4.3b
trend-tells -> signal_engine/orb entry -> stop/target/time-stop/EOD exit
-- bar-by-bar over historical candles, using the SAME production modules
live trading calls (signal_engine, regime_detector, trend_tells, orb,
inplay, paper_broker's cost model). This is not a parallel re-implementation
of the strategy: decision-fidelity (§6.2) only means something if the
backtest and the brain share code, not a lookalike.

No LLM regime layer exists in the live trading pipeline as of 2026-07-23
(grepped: zero llm/anthropic/openai calls in the codebase) — the §8 "LLM
Brain backtestability" open question referred to a component that isn't
built yet. regime_detector is 100% mechanical (ADX + nifty direction +
multi-timeframe), so this harness already replays the real thing, not an
approximation. Revisit if/when an LLM regime layer is actually added.

Data source: NOT wired to a live data pull here. Feed it historical
candles (get them via kite_client.KiteClient.get_historical_data, or
however they're sourced) shaped like the `candles` table rows. Multi-year
2020-2022 regime data needs the official Kite Connect historical API
subscription (VISION §3c) — that's an external/account decision, not a
code gap; nothing here can substitute for having the data.

Usage sketch:
    from backtest import run_backtest
    result = run_backtest(
        symbol_days={'INFY': {'2026-07-22': {'5minute': [...], '15minute': [...],
                                              '60minute': [...]}}},
        nifty_days={'2026-07-22': {'5minute': [...]}},
        entry_archetype='CONFLUENCE',   # or 'ORB'
        exit_style='FIXED_TARGET',      # or 'TRAIL_TO_CLOSE'
    )
"""
from datetime import datetime, timedelta

import pytz

import config
import inplay
import orb
import trend_tells
from brain import _invert_for_short
from paper_broker import _zerodha_intraday_charges
from regime_detector import RegimeDetector
from risk_manager import RiskManager
from signal_engine import SignalEngine

IST = pytz.timezone('Asia/Kolkata')

# VISION §5 gate 6 — the three regime periods the hypothesis must clear.
# Approximate, widely-used windows; not a precision claim, just enough to
# bucket trades for the per-regime PF check.
REGIME_PERIODS = {
    'CRASH_2020': ('2020-02-01', '2020-04-30'),
    'BULL_2021': ('2021-01-01', '2021-12-31'),
    'CHOP_2022': ('2022-01-01', '2022-12-31'),
}

# §4.2 time-stop: a position that hasn't moved this many R in its favor by
# TIME_STOP_MIN is scratched rather than left to become an eventual loser.
MEANINGFUL_MOVE_R = 0.3

# §4.7 "max 2-3 trades per day (start: 3)" — the base strategy limit, not
# the data-collection pacing knobs (config.DATA_MAX_TRADES_PER_DAY etc.),
# which exist to widen the paper run for training-data volume, not to
# define the strategy under test here.
MAX_TRADES_PER_SYMBOL_DAY = 3


def classify_regime_period(iso_date: str):
    for name, (start, end) in REGIME_PERIODS.items():
        if start <= iso_date <= end:
            return name
    return None


def _parse_ts(ts: str) -> datetime:
    dt = datetime.fromisoformat(ts.replace('Z', '+00:00'))
    return dt.astimezone(IST) if dt.tzinfo else IST.localize(dt)


def _day_key(candles: list) -> dict:
    out = {}
    for c in candles or []:
        d = (c.get('timestamp') or c.get('ts') or '')[:10]
        if d:
            out.setdefault(d, []).append(c)
    return out


def decision_fidelity_replay(candles_5min, candles_15min, candles_1hour,
                             live_price, symbol, nifty_direction,
                             nifty_change_percent, now):
    """§6.2 interface: given the same market snapshot a live decision saw,
    return what the backtest logic would have decided. Thin wrapper — it
    IS signal_engine.generate_signal, not a copy, so a live-vs-replay
    mismatch is a real divergence, not an artifact of two implementations."""
    engine = SignalEngine()
    return engine.generate_signal(
        candles_5min=candles_5min, candles_15min=candles_15min,
        candles_1hour=candles_1hour, live_price=live_price, symbol=symbol,
        nifty_direction=nifty_direction, nifty_change_percent=nifty_change_percent,
        now=now,
    )


def _entry_signal(archetype, engine, candles_5min, candles_15min, candles_1hour,
                  live_price, symbol, nifty_direction, nifty_change_percent, now):
    if archetype == 'ORB':
        or_stats = inplay.opening_range_stats(candles_5min)
        sig = orb.orb_signal(live_price, or_stats)
        if sig['action'] != 'HOLD' and sig['confidence'] < config.ORB_MIN_CONFIDENCE:
            sig = dict(sig, action='HOLD')
        return sig
    return engine.generate_signal(
        candles_5min=candles_5min, candles_15min=candles_15min,
        candles_1hour=candles_1hour, live_price=live_price, symbol=symbol,
        nifty_direction=nifty_direction, nifty_change_percent=nifty_change_percent,
        now=now,
    )


def _tells_permit(direction, candles_5min_today, prev_close, today_open,
                  live_price, avg_range):
    result = trend_tells.evaluate(
        direction=direction, candles_5min=candles_5min_today,
        prev_close=prev_close, today_open=today_open, current_price=live_price,
        today_range=trend_tells.session_range(candles_5min_today),
        avg_range=avg_range,
        # Breadth/sector data isn't wired into this harness (v1) — same
        # "more tells abstain" degradation brain.py's own best-effort
        # snapshot uses when it's unavailable live.
        advancers=None, decliners=None, sector_aligned=None,
    )
    return result['permits_entry']


def _walk_forward_exit(direction, entry, stop, target, candles_after,
                       entry_ts, exit_style, time_stop_min):
    """Bar-by-bar exit simulation. Priority per bar: EOD square-off, stop,
    target/trailing-stop, then time-stop. Mirrors decision_outcomes.py's
    stop-first convention (same-bar stop+target ambiguity resolves
    conservatively as stop-first) plus the two exits Track C doesn't need:
    time-stop and EOD square-off."""
    square_off_min = (config.MARKET_CLOSE_HOUR * 60 + config.MARKET_CLOSE_MINUTE)
    trail_distance = abs(entry - stop)
    trail_stop = stop

    for c in candles_after:
        ts = _parse_ts(c.get('timestamp') or c['ts'])
        lo, hi, close = float(c['low']), float(c['high']), float(c['close'])
        minute_of_day = ts.hour * 60 + ts.minute
        elapsed_min = (ts - entry_ts).total_seconds() / 60.0

        if minute_of_day >= square_off_min:
            return close, 'SESSION_END', ts

        if direction == 'LONG':
            if lo <= stop:
                return stop, 'STOP_HIT', ts
            if exit_style == 'TRAIL_TO_CLOSE':
                # Check against the trail level set by PRIOR bars before
                # ratcheting it with this bar's own high — using this bar's
                # high to justify a stop this same bar's low broke is
                # look-ahead bias (unknowable intrabar sequencing).
                if lo <= trail_stop:
                    return trail_stop, 'TRAIL_STOP_HIT', ts
                trail_stop = max(trail_stop, hi - trail_distance)
            elif hi >= target:
                return target, 'TARGET_HIT', ts
        else:  # SHORT
            if hi >= stop:
                return stop, 'STOP_HIT', ts
            if exit_style == 'TRAIL_TO_CLOSE':
                if hi >= trail_stop:
                    return trail_stop, 'TRAIL_STOP_HIT', ts
                trail_stop = min(trail_stop, lo + trail_distance)
            elif lo <= target:
                return target, 'TARGET_HIT', ts

        if elapsed_min >= time_stop_min:
            r = ((close - entry) / trail_distance if direction == 'LONG'
                 else (entry - close) / trail_distance)
            if r < MEANINGFUL_MOVE_R:
                return close, 'TIME_STOP', ts

    if candles_after:
        last = candles_after[-1]
        return float(last['close']), 'SESSION_END', _parse_ts(last.get('timestamp') or last['ts'])
    return entry, 'NO_DATA', entry_ts


def simulate_symbol_day(symbol, day_candles_5min, day_candles_15min,
                        day_candles_1hour, nifty_day_5min,
                        entry_archetype='CONFLUENCE', exit_style='FIXED_TARGET',
                        time_stop_min=None, capital=25000.0):
    """One symbol, one day. Walks 5-min bars from 09:30 IST (opening range
    formed) to the no-new-entries cutoff, one open position at a time
    (§4.7), gated by §4.3b trend-tells. Returns a list of simulated trades."""
    time_stop_min = time_stop_min if time_stop_min is not None else config.TIME_STOP_MIN
    engine = SignalEngine()
    risk_mgr = RiskManager()
    trades = []

    days = sorted(_day_key(day_candles_5min).keys())
    if not days:
        return trades
    today = days[-1]
    today_5min = [c for c in day_candles_5min
                  if (c.get('timestamp') or c.get('ts') or '')[:10] == today]
    if len(today_5min) < 4:
        return trades

    prior_days = [c for c in day_candles_5min
                  if (c.get('timestamp') or c.get('ts') or '')[:10] != today]
    prev_close = prior_days[-1]['close'] if prior_days else None
    today_open = today_5min[0]['open']
    prior_ranges = []
    for d in days[:-1]:
        dc = [c for c in day_candles_5min if (c.get('timestamp') or c.get('ts') or '')[:10] == d]
        r = trend_tells.session_range(dc)
        if r is not None:
            prior_ranges.append(r)
    avg_range = sum(prior_ranges) / len(prior_ranges) if prior_ranges else None

    start_min = config.MARKET_START_TRADING_HOUR * 60 + config.MARKET_START_TRADING_MINUTE
    no_entries_min = config.MARKET_NO_NEW_ENTRIES_HOUR * 60 + config.MARKET_NO_NEW_ENTRIES_MINUTE

    # §4.7: one open position at a time, capped trades/day. Applied per
    # symbol here (v1 simplification) — true session-wide "one position
    # across the whole book" needs a multi-symbol day loop; note this if
    # tightening the harness before gate #6 sign-off.
    busy_until = None
    for i in range(3, len(today_5min)):  # need >=15min (3 bars) for opening range
        bar = today_5min[i]
        bar_ts = _parse_ts(bar.get('timestamp') or bar['ts'])
        minute_of_day = bar_ts.hour * 60 + bar_ts.minute
        if len(trades) >= MAX_TRADES_PER_SYMBOL_DAY:
            break
        if (busy_until and bar_ts <= busy_until) or minute_of_day < start_min or minute_of_day > no_entries_min:
            continue

        candles_5min_so_far = day_candles_5min[:len(prior_days) + i + 1]
        candles_15min_so_far = [c for c in (day_candles_15min or [])
                                if (c.get('timestamp') or c.get('ts')) <= (bar.get('timestamp') or bar['ts'])]
        candles_1h_so_far = [c for c in (day_candles_1hour or [])
                             if (c.get('timestamp') or c.get('ts')) <= (bar.get('timestamp') or bar['ts'])]
        nifty_so_far = [c for c in (nifty_day_5min or [])
                        if (c.get('timestamp') or c.get('ts')) <= (bar.get('timestamp') or bar['ts'])]
        live_price = float(bar['close'])

        if len(nifty_so_far) >= 2:
            n0, n1 = float(nifty_so_far[0]['close']), float(nifty_so_far[-1]['close'])
            nifty_change_percent = round((n1 - n0) / n0 * 100, 3) if n0 else 0.0
        else:
            nifty_change_percent = 0.0
        nifty_direction = ('BULLISH' if nifty_change_percent >= 0.5 else
                           'BEARISH' if nifty_change_percent <= -0.5 else 'SIDEWAYS')

        signal = _entry_signal(
            entry_archetype, engine, candles_5min_so_far, candles_15min_so_far,
            candles_1h_so_far, live_price, symbol, nifty_direction,
            nifty_change_percent, now=bar_ts)

        action = signal.get('action')
        if action not in ('BUY', 'SELL'):
            continue

        direction = 'LONG' if action == 'BUY' else 'SHORT'
        tells_direction = 'UP' if direction == 'LONG' else 'DOWN'
        if not _tells_permit(tells_direction, today_5min[:i + 1], prev_close,
                             today_open, live_price, avg_range):
            continue

        stop, target = signal['stop_loss'], signal['target']
        if direction == 'SHORT' and entry_archetype != 'ORB':
            stop, target = _invert_for_short(live_price, stop, target)
        if stop is None or target is None:
            continue

        quantity = risk_mgr.calculate_position_size(
            capital=capital, live_price=live_price, confidence=signal['confidence'],
            stop_loss_price=stop, target_price=target, historical_win_rate=None,
            n_trades=0, symbol=symbol,
        )
        if quantity <= 0:
            continue

        entry_fill = live_price * (1 + config.PAPER_SLIPPAGE_PCT / 100
                                   if direction == 'LONG'
                                   else 1 - config.PAPER_SLIPPAGE_PCT / 100)
        entry_charges = _zerodha_intraday_charges(
            'BUY' if direction == 'LONG' else 'SELL', entry_fill, quantity)

        candles_after = today_5min[i + 1:]
        exit_price, exit_reason, exit_ts = _walk_forward_exit(
            direction, live_price, stop, target, candles_after, bar_ts,
            exit_style, time_stop_min)

        exit_side = 'SELL' if direction == 'LONG' else 'BUY'
        exit_fill = exit_price * (1 - config.PAPER_SLIPPAGE_PCT / 100
                                  if direction == 'LONG'
                                  else 1 + config.PAPER_SLIPPAGE_PCT / 100)
        exit_charges = _zerodha_intraday_charges(exit_side, exit_fill, quantity)

        gross = ((exit_fill - entry_fill) if direction == 'LONG'
                 else (entry_fill - exit_fill)) * quantity
        pnl = gross - entry_charges - exit_charges
        risk_rupees = abs(entry_fill - stop) * quantity
        r_multiple = round(pnl / risk_rupees, 3) if risk_rupees > 0 else None

        trades.append({
            'symbol': symbol, 'date': today, 'direction': direction,
            'archetype': entry_archetype, 'exit_style': exit_style,
            'entry_price': round(entry_fill, 2), 'stop': stop, 'target': target,
            'quantity': quantity, 'exit_price': round(exit_fill, 2),
            'exit_reason': exit_reason, 'pnl': round(pnl, 2),
            'r_multiple': r_multiple, 'entry_ts': bar_ts.isoformat(),
            'exit_ts': exit_ts.isoformat() if hasattr(exit_ts, 'isoformat') else None,
            'regime_period': classify_regime_period(today),
        })
        # One open position at a time (§4.7) — resume scanning only after
        # this trade's exit bar.
        busy_until = exit_ts if hasattr(exit_ts, 'isoformat') else bar_ts

    return trades


def _max_drawdown(trades: list) -> float:
    equity, peak, max_dd = 0.0, 0.0, 0.0
    for t in sorted(trades, key=lambda x: x['entry_ts']):
        equity += t['pnl']
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
    return round(max_dd, 2)


def _profit_factor(trades: list):
    gross_win = sum(t['pnl'] for t in trades if t['pnl'] > 0)
    gross_loss = -sum(t['pnl'] for t in trades if t['pnl'] < 0)
    if gross_loss == 0:
        return None
    return round(gross_win / gross_loss, 3)


def run_backtest(symbol_days: dict, nifty_days: dict, entry_archetype='CONFLUENCE',
                 exit_style='FIXED_TARGET', time_stop_min=None, capital=25000.0):
    """symbol_days: {symbol: {date: {'5minute': [...], '15minute': [...],
    '60minute': [...]}}}. nifty_days: {date: {'5minute': [...]}}.

    Returns {trades, profit_factor, win_rate, max_drawdown, per_regime:
    {period: {profit_factor, n}}}. Aggregate PF/drawdown are net of the
    real Zerodha MIS cost model (paper_broker._zerodha_intraday_charges) —
    see VISION §6.1 for the go/kill thresholds these are checked against."""
    all_trades = []
    for symbol, by_date in symbol_days.items():
        dates = sorted(by_date.keys())
        for idx, date in enumerate(dates):
            window_dates = dates[max(0, idx - 20):idx + 1]  # ~20-day RVOL/range baseline
            day_5min = [c for d in window_dates for c in by_date[d].get('5minute', [])]
            day_15min = [c for d in window_dates for c in by_date[d].get('15minute', [])]
            day_1h = [c for d in window_dates for c in by_date[d].get('60minute', [])]
            nifty_5min = nifty_days.get(date, {}).get('5minute', [])
            all_trades.extend(simulate_symbol_day(
                symbol, day_5min, day_15min, day_1h, nifty_5min,
                entry_archetype=entry_archetype, exit_style=exit_style,
                time_stop_min=time_stop_min, capital=capital,
            ))

    wins = sum(1 for t in all_trades if t['pnl'] > 0)
    per_regime = {}
    for period in REGIME_PERIODS:
        bucket = [t for t in all_trades if t['regime_period'] == period]
        if bucket:
            per_regime[period] = {
                'n': len(bucket), 'profit_factor': _profit_factor(bucket),
                'pnl': round(sum(t['pnl'] for t in bucket), 2),
            }

    return {
        'trades': all_trades,
        'n': len(all_trades),
        'profit_factor': _profit_factor(all_trades),
        'win_rate': round(wins / len(all_trades), 3) if all_trades else None,
        'total_pnl': round(sum(t['pnl'] for t in all_trades), 2),
        'max_drawdown': _max_drawdown(all_trades),
        'per_regime': per_regime,
        'entry_archetype': entry_archetype,
        'exit_style': exit_style,
    }
