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

    # Warm holdings instrument tokens FIRST. get_candles resolves an uncached
    # symbol's token via /quote, which 400s on a retail enctoken — so a cold
    # MarketData (the session-start catch-up + grade_advice CLI both build one)
    # fetched no candles for any holding, and every due row was wrongly counted
    # not-due and never graded (2026-08 backfill stalled at +4). Holdings API
    # carries instrument_token and works on a retail token, so this fills the
    # cache and makes grading self-sufficient regardless of caller.
    try:
        market_data.refresh_holdings_cache()
    except Exception as e:
        print(f"[backtest] holdings warm failed (non-fatal): {e}")

    # One Nifty fetch serves every row (index token pinned; best-effort).
    nifty_candles = []
    try:
        market_data._instrument_cache['NSE:NIFTY 50'] = 256265
        nifty_candles = market_data.get_candles('NSE:NIFTY 50', 'day', 400) or []
    except Exception as e:
        print(f"[backtest] nifty benchmark unavailable (non-fatal): {e}")

    candle_cache = {}
    evaluated = 0
    not_due = 0
    errors = 0
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
                    not_due += 1                  # candles short of the horizon
                    continue
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
            errors += 1
            print(f"[backtest] {row.get('symbol')} skipped: {e}")
    # Always log the pass tally — a due-but-never-graded backlog (the 2026-08
    # starvation: 38 matured rows silently unevaluated) is only diagnosable if
    # every pass reports due/graded/not-due/errors, not just the happy path.
    print(f"[backtest] pass: queued={len(queue)} graded={evaluated} "
          f"not_due={not_due} errors={errors} "
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


# --- Factor attribution ---------------------------------------------------
# The advisor's 7 scoring weights (EMA200=20, EMA50=15, consistency=15,
# momentum=20, ADX=10, relative-strength=20, news=10) are hand-picked
# priors. This measures, once enough calls are graded, whether each factor
# actually separated right calls from wrong ones — the evidence to reweight
# on, instead of guessing. Each factor maps a graded row to a bucket; a
# factor that predicts shows a real hit-rate spread between its buckets.

def _bucket_ema200(row, ind):
    price, ema = row.get('last_price'), ind.get('ema_200')
    if not price or not ema:
        return None
    return 'price_above_200EMA' if price > ema else 'price_below_200EMA'


def _bucket_ema50(row, ind):
    price, ema = row.get('last_price'), ind.get('ema_50')
    if not price or not ema:
        return None
    return 'price_above_50EMA' if price > ema else 'price_below_50EMA'


def _bucket_consistency(row, ind):
    c = ind.get('trend_consistency_pct')
    if c is None:
        return None
    return 'held_above_50EMA' if c >= 50 else 'kept_losing_50EMA'


def _bucket_rel_strength(row, ind):
    rs = ind.get('relative_strength_vs_nifty')
    if rs is None:
        return None
    if rs >= 5:
        return 'strong_outperform'
    if rs <= -5:
        return 'strong_underperform'
    return 'inline_with_nifty'


def _bucket_rsi(row, ind):
    rsi = ind.get('rsi_14')
    if rsi is None:
        return None
    if rsi <= 32:
        return 'oversold'
    if rsi >= 70:
        return 'overbought'
    return 'neutral'


def _bucket_adx(row, ind):
    adx = ind.get('adx')
    if adx is None:
        return None
    return 'trending_ADX>=20' if adx >= 20 else 'choppy_ADX<20'


def _bucket_trigger(row, ind):
    t = (row.get('trigger_type') or '').upper()
    return t if t in ('MACRO', 'MICRO') else None


def _bucket_volume(row, ind):
    v = ind.get('volume_trend_ratio')
    if v is None:
        return None
    return 'volume_building' if v >= 1.3 else 'volume_flat'


def _bucket_weekly_alignment(row, ind):
    a = ind.get('daily_weekly_alignment')
    return a if a in ('ALIGNED_UP', 'ALIGNED_DOWN', 'CONFLICT', 'NEUTRAL') else None


_FACTORS = {
    'ema200_position': _bucket_ema200,
    'ema50_position': _bucket_ema50,
    'trend_consistency': _bucket_consistency,
    'relative_strength': _bucket_rel_strength,
    'rsi_zone': _bucket_rsi,
    'adx_regime': _bucket_adx,
    'trigger_type': _bucket_trigger,
    'volume_trend': _bucket_volume,
    # Step-2 candidate (2026-07-23): does daily/weekly agreement predict?
    # Logged + graded here before it earns a trend_score weight.
    'daily_weekly_alignment': _bucket_weekly_alignment,
}


def factor_attribution(rows: list, min_bucket_n: int = 5) -> dict:
    """Per-factor hit-rate/alpha breakdown over graded advice rows. Pure —
    feed it db.get_evaluated_advice_with_features() output (or synthetic
    rows in tests).

    For each factor, buckets the calls it applies to and reports each
    bucket's n / hit-rate / avg alpha. `separation_pct` = the hit-rate gap
    between the factor's best and worst sufficiently-sampled bucket — a
    factor that genuinely predicts shows a wide gap; near-zero means the
    factor isn't earning its weight. Factors are ranked by separation so
    the reweighting conversation starts from evidence, not the current
    hand-picked priors. Buckets under `min_bucket_n` are computed but
    flagged low-n and excluded from the separation ranking."""
    judged = [r for r in rows if r.get('outcome_correct') is not None]
    factors = {}
    for name, fn in _FACTORS.items():
        buckets = {}
        for r in judged:
            ind = r.get('indicators') or {}
            label = fn(r, ind)
            if label is None:
                continue
            b = buckets.setdefault(label, {'n': 0, 'hits': 0, 'alphas': []})
            b['n'] += 1
            b['hits'] += 1 if r['outcome_correct'] else 0
            if r.get('outcome_vs_nifty_pct') is not None:
                b['alphas'].append(float(r['outcome_vs_nifty_pct']))
        for b in buckets.values():
            b['hit_rate_pct'] = round(b['hits'] / b['n'] * 100, 1) if b['n'] else None
            b['avg_alpha_pct'] = (round(sum(b['alphas']) / len(b['alphas']), 2)
                                  if b['alphas'] else None)
            b['low_n'] = b['n'] < min_bucket_n
            del b['alphas']
        rated = [b['hit_rate_pct'] for b in buckets.values()
                 if not b['low_n'] and b['hit_rate_pct'] is not None]
        separation = round(max(rated) - min(rated), 1) if len(rated) >= 2 else None
        factors[name] = {'buckets': buckets, 'separation_pct': separation}

    ranked = sorted(
        (n for n, f in factors.items() if f['separation_pct'] is not None),
        key=lambda n: factors[n]['separation_pct'], reverse=True)

    return {
        'graded_calls': len(judged),
        'factors': factors,
        'ranked_by_separation': ranked,
    }


# --- Confidence calibration (advisor god-mode Pillar 1) -------------------
# The advisor's confidence is a hand-picked heuristic: min(90, 50+|score|//2).
# A *calibrated* confidence of 70 should mean 70% of such calls come true. This
# builds the reliability curve — predicted confidence vs empirical hit-rate —
# so the heuristic can eventually be REPLACED by a measured mapping instead of
# guessed. DARK by construction: computed, stored, and dark-attached to rows,
# but NEVER folded into the live confidence/verdict until the curve is both
# monotonic and rests on enough calls (VISION §7: change on evidence, and n≈21
# in one regime is nowhere near that — the shrinkage below keeps small buckets
# honest by pulling them toward the base rate).

_CALIB_BINS = [(50, 60), (60, 70), (70, 80), (80, 90), (90, 101)]


def calibration_curve(rows: list, prior_strength: int = 10) -> dict:
    """Reliability curve over graded advice: bucket calls by their stated
    confidence, compare each bucket's predicted confidence to its empirical
    hit-rate. Pure. `calibrated_pct` is the hit-rate shrunk toward the base
    rate by a Beta(prior_strength) prior, so a 2-call bucket can't swing the
    mapping. `ece_pct` = sample-weighted mean gap between predicted and
    empirical (0 = perfectly calibrated); `monotonic` = does empirical
    hit-rate rise with confidence (the property a usable confidence needs)."""
    judged = [r for r in rows if r.get('outcome_correct') is not None
              and r.get('confidence') is not None]
    n = len(judged)
    if not n:
        return {'graded_calls': 0, 'base_rate_pct': None, 'bins': [],
                'ece_pct': None, 'monotonic': None, 'prior_strength': prior_strength}

    base = sum(1 for r in judged if r['outcome_correct']) / n
    bins = []
    for lo, hi in _CALIB_BINS:
        b = [r for r in judged if lo <= r['confidence'] < hi]
        if not b:
            continue
        bn = len(b)
        hits = sum(1 for r in b if r['outcome_correct'])
        pred = sum(r['confidence'] for r in b) / bn
        shrunk = (hits + prior_strength * base) / (bn + prior_strength)
        bins.append({
            'lo': lo, 'hi': hi, 'label': f'{lo}-{hi - 1}', 'n': bn,
            'predicted_pct': round(pred, 1),
            'empirical_hit_pct': round(hits / bn * 100, 1),
            'calibrated_pct': round(shrunk * 100, 1),
            'low_n': bn < 5,
        })

    ece = sum(b['n'] * abs(b['predicted_pct'] - b['empirical_hit_pct'])
              for b in bins) / n
    emp = [b['empirical_hit_pct'] for b in bins]
    monotonic = (all(x <= y for x, y in zip(emp, emp[1:]))
                 if len(emp) >= 2 else None)
    return {
        'graded_calls': n,
        'base_rate_pct': round(base * 100, 1),
        'bins': bins,
        'ece_pct': round(ece, 1),
        'monotonic': monotonic,
        'prior_strength': prior_strength,
    }


def calibrated_confidence(confidence, table: dict):
    """Dark lookup: raw heuristic confidence → the measured (shrunk) hit-rate
    for its bucket. Returns (value, low_n). Falls back to the raw confidence
    (flagged low_n) whenever there's no table or no matching bucket — so it is
    always safe to attach to a row without changing behaviour."""
    if confidence is None:
        return None, True
    if not table or not table.get('bins'):
        return confidence, True
    for b in table['bins']:
        if b['lo'] <= confidence < b['hi']:
            return b['calibrated_pct'], b['low_n']
    return confidence, True
