"""Advisor accountability — judge every stored verdict against what the stock
actually did next (ADVISOR_ACCOUNTABILITY).

Each portfolio_advice row is evaluated once, after a fixed horizon of trading
days, against two questions:
  1. raw forward return from the advice-time price
  2. was the CALL right — HOLD is right if the stock rose; SELL/TRIM/
     SELL_ON_BOUNCE are right if it fell (the exit avoided a drawdown)
plus a Nifty-relative framing (beating a do-nothing index hold is the honest
bar, not just "went up in a bull tape").

The aggregate (get_track_record_summary) is the advisor's public track record:
hit rate, average alpha per call, and a rupee number — what following the
exit calls actually saved/cost vs sitting still, sized by the real position
quantities stored on each row.

ADVISORY ONLY: reads candles, writes outcome columns. No order path.
"""
from datetime import datetime

import pytz

import config
import database as db

IST = pytz.timezone('Asia/Kolkata')

# Verdicts judged as "exit calls" — right when the price then fell. TRIM is
# a half-exit, so its rupee impact is weighted at half the position.
_EXIT_VERDICTS = {'SELL': 1.0, 'SELL_ON_BOUNCE': 1.0, 'TRIM': 0.5}


def evaluate_verdict(row: dict, forward_return_pct: float,
                     nifty_return_pct: float = None) -> dict:
    """Pure judgment of one advice row given the realized forward return.
    Returns the outcome column dict (evaluated_at added by the caller).
    INSUFFICIENT rows get outcome_correct=None — a no-call can't be right
    or wrong, but it leaves the work queue."""
    verdict = row.get('verdict')
    vs_nifty = (round(forward_return_pct - nifty_return_pct, 2)
                if nifty_return_pct is not None else None)
    if verdict == 'HOLD':
        correct = forward_return_pct > 0
    elif verdict in _EXIT_VERDICTS:
        correct = forward_return_pct < 0
    else:                                   # INSUFFICIENT / unknown
        correct = None
    return {
        'outcome_return_pct': round(forward_return_pct, 2),
        'outcome_vs_nifty_pct': vs_nifty,
        'outcome_correct': correct,
    }


def _bars_from(candles: list, run_date: str) -> list:
    """Daily bars dated ON or AFTER run_date — the advice ran pre-open-ish on
    run_date, so that day's close is the first forward observation."""
    out = []
    for c in candles or []:
        ts = str(c.get('timestamp') or '')[:10]
        if ts >= run_date and c.get('close') is not None:
            out.append(c)
    return out


def horizon_for(row: dict, default_days: int = None) -> int:
    """Evaluation horizon by trigger type. MACRO calls (EMA200 / relative-
    strength driven) get the long runway — judging a 200-day-structure
    thesis at 10 days penalizes exactly the calls that need patience.
    Legacy rows (no trigger_type) keep the original horizon unchanged."""
    if (row.get('trigger_type') or '').upper() == 'MACRO':
        return config.ADVISOR_BACKTEST_MACRO_HORIZON_DAYS
    return default_days or config.ADVISOR_BACKTEST_HORIZON_DAYS


def _index_return_between(nifty_candles: list, run_date: str,
                          end_date: str):
    """Nifty return over the stock's EXACT calendar window [run_date,
    end_date] — not 'the index's own N bars'. When the stock skips sessions
    (illiquidity, suspension), its N trading days span more calendar days
    than the index's N; counting bars on both sides silently compares
    different windows and corrupts alpha. None when the index has <2 bars
    inside the window."""
    bars = [c for c in nifty_candles or []
            if run_date <= str(c.get('timestamp') or '')[:10] <= end_date
            and c.get('close') is not None]
    if len(bars) < 2:
        return None
    n0, n1 = float(bars[0]['close']), float(bars[-1]['close'])
    return (n1 - n0) / n0 * 100 if n0 else None


def run_backtest_pass(market_data, horizon_days: int = None) -> int:
    """Evaluate every due, unevaluated advice row. Due = the symbol has that
    row's horizon of daily bars after run_date (trading-day horizon measured
    on the chart itself — 10 days for MICRO-triggered calls, 30 for MACRO).
    Per-row failures skip that row. Returns rows evaluated."""
    today = datetime.now(IST).date().isoformat()
    queue = db.get_unevaluated_advice(today)
    if not queue:
        return 0

    # One Nifty fetch serves every row (index token pinned; best-effort).
    nifty_candles = []
    try:
        market_data._instrument_cache['NSE:NIFTY 50'] = 256265
        nifty_candles = market_data.get_candles('NSE:NIFTY 50', 'day', 400) or []
    except Exception as e:
        print(f"[backtest] nifty benchmark unavailable (non-fatal): {e}")

    candle_cache = {}
    evaluated = 0
    for row in queue:
        try:
            run_date, symbol = row['run_date'], row['symbol']
            base_price = float(row.get('last_price') or 0)
            outcome = None

            if row.get('verdict') == 'INSUFFICIENT' or not base_price:
                # no-call: clear it from the queue, judged as neither
                outcome = {'outcome_return_pct': None,
                           'outcome_vs_nifty_pct': None,
                           'outcome_correct': None,
                           'evaluation_horizon_days': None}
            else:
                horizon = horizon_for(row, horizon_days)
                if symbol not in candle_cache:
                    key = f'NSE:{symbol}'
                    token = (config.NIFTY500_INSTRUMENT_TOKENS.get(key)
                             or market_data._instrument_cache.get(key))
                    if token:
                        market_data._instrument_cache[key] = token
                    candle_cache[symbol] = market_data.get_candles(
                        key, 'day', 400) or []
                bars = _bars_from(candle_cache[symbol], run_date)
                if len(bars) < horizon:
                    continue                      # not due yet
                end_bar = bars[horizon - 1]
                price_then = float(end_bar['close'])
                fwd = (price_then - base_price) / base_price * 100

                # Alpha window = the STOCK's realized calendar span, so the
                # index is measured over the same dates even when the stock
                # skipped sessions.
                end_date = str(end_bar.get('timestamp') or '')[:10]
                nifty_ret = _index_return_between(
                    nifty_candles, run_date, end_date)
                outcome = evaluate_verdict(row, fwd, nifty_ret)
                outcome['evaluation_horizon_days'] = horizon

            outcome['evaluated_at'] = datetime.now(IST).isoformat()
            if db.update_advice_outcome(run_date, symbol, outcome):
                evaluated += 1
        except Exception as e:
            print(f"[backtest] {row.get('symbol')} skipped: {e}")
    if evaluated:
        print(f"[backtest] evaluated {evaluated} advice rows "
              f"(MICRO {config.ADVISOR_BACKTEST_HORIZON_DAYS}d / "
              f"MACRO {config.ADVISOR_BACKTEST_MACRO_HORIZON_DAYS}d horizons)")
    return evaluated


def get_track_record_summary() -> dict:
    """The advisor's single accountability read: how the calls have actually
    gone. advice_value_inr = rupees saved (+) or cost (−) by following the
    exit calls vs doing nothing, sized by each row's real quantity — HOLDs
    are the do-nothing baseline and contribute 0 by construction."""
    rows = db.get_evaluated_advice()
    judged = [r for r in rows if r.get('outcome_correct') is not None]
    if not judged:
        return {'evaluated_calls': 0, 'hit_rate_pct': None,
                'avg_return_pct': None, 'avg_alpha_pct': None,
                'advice_value_inr': 0.0, 'by_verdict': {}}

    hits = sum(1 for r in judged if r['outcome_correct'])
    returns = [float(r['outcome_return_pct']) for r in judged
               if r.get('outcome_return_pct') is not None]
    alphas = [float(r['outcome_vs_nifty_pct']) for r in judged
              if r.get('outcome_vs_nifty_pct') is not None]

    value = 0.0
    by_verdict = {}
    for r in judged:
        v = r.get('verdict')
        s = by_verdict.setdefault(v, {'calls': 0, 'hits': 0})
        s['calls'] += 1
        s['hits'] += 1 if r['outcome_correct'] else 0
        weight = _EXIT_VERDICTS.get(v)
        if weight and r.get('outcome_return_pct') is not None:
            qty = float(r.get('quantity') or 0)
            base = float(r.get('last_price') or 0)
            # exit call: money saved = what the kept position would have lost
            value += weight * qty * base * (-float(r['outcome_return_pct']) / 100)

    # Split by what the USER did with each call (Telegram Accept/Decline) —
    # the honest read of whether following the advisor beats ignoring it.
    by_decision = {}
    for r in judged:
        d = r.get('user_decision') or 'ignored'
        s = by_decision.setdefault(d, {'calls': 0, 'hits': 0, 'alphas': []})
        s['calls'] += 1
        s['hits'] += 1 if r['outcome_correct'] else 0
        if r.get('outcome_vs_nifty_pct') is not None:
            s['alphas'].append(float(r['outcome_vs_nifty_pct']))
    for s in by_decision.values():
        s['hit_rate_pct'] = round(s['hits'] / s['calls'] * 100, 1)
        s['avg_alpha_pct'] = (round(sum(s['alphas']) / len(s['alphas']), 2)
                              if s['alphas'] else None)
        del s['alphas']

    return {
        'evaluated_calls': len(judged),
        'hit_rate_pct': round(hits / len(judged) * 100, 1),
        'avg_return_pct': round(sum(returns) / len(returns), 2) if returns else None,
        'avg_alpha_pct': round(sum(alphas) / len(alphas), 2) if alphas else None,
        'advice_value_inr': round(value, 2),
        'by_verdict': by_verdict,
        'by_decision': by_decision,
    }
