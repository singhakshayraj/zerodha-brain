"""Opening-Range Breakout archetype — ENGINEERING_SPEC §5 step 5A.

The second entry archetype alongside the existing indicator-confluence
engine (§5 step 5B). A clean break of the 15-minute opening range, in the
direction of the break, with confirmation from opening-range RVOL and the
day's gap. Pure — feed it a live price + inplay.opening_range_stats output.

Natural ORB risk: stop at the far side of the opening range (a break that
fails back inside is invalidated); target = a measured move (one OR range
projected from the break). When level-anchored stops are enabled the brain
prefers those; ORB's own levels are the fallback.

Returns a signal dict shaped like signal_engine.generate_signal's subset:
{action, confidence, reasons, skip_reasons, stop_loss, target,
 risk_reward_ratio, archetype:'ORB', or_high, or_low, break_strength}.
"""

import config

_HOLD = 'HOLD'


def _hold(reason: str) -> dict:
    return {'action': _HOLD, 'confidence': 0, 'reasons': [],
            'skip_reasons': [reason], 'stop_loss': None, 'target': None,
            'risk_reward_ratio': None, 'archetype': 'ORB'}


def orb_signal(live_price: float, or_stats: dict,
               break_buffer_frac: float = None) -> dict:
    """or_stats: inplay.opening_range_stats() output (or_high/or_low/or_rvol/
    gap_pct). Returns an ORB signal, or HOLD when price is inside the range
    or the OR is undefined."""
    break_buffer_frac = (config.ORB_BREAK_BUFFER_FRAC
                         if break_buffer_frac is None else break_buffer_frac)
    if not or_stats or not live_price or live_price <= 0:
        return _hold('No opening-range data')

    or_high = or_stats.get('or_high')
    or_low = or_stats.get('or_low')
    if not or_high or not or_low:
        return _hold('Incomplete opening range')

    or_range = or_high - or_low
    if or_range <= 0:
        return _hold('Degenerate opening range')

    buf = or_range * break_buffer_frac
    rvol = or_stats.get('or_rvol')
    gap_pct = or_stats.get('gap_pct')

    if live_price > or_high + buf:
        action = 'BUY'
        break_strength = (live_price - or_high) / or_range
        stop = round(or_low, 2)                       # far side of the range
        target = round(or_high + or_range, 2)         # 1× measured move
        gap_aligned = (gap_pct or 0) > 0
    elif live_price < or_low - buf:
        action = 'SELL'
        break_strength = (or_low - live_price) / or_range
        stop = round(or_high, 2)
        target = round(or_low - or_range, 2)
        gap_aligned = (gap_pct or 0) < 0
    else:
        return _hold('Price inside opening range')

    # Mechanical confidence: base + RVOL confirmation + gap alignment +
    # decisive-break bonus, capped.
    confidence = 55
    reasons = [f"ORB {action}: broke {'above' if action == 'BUY' else 'below'} "
               f"OR ({or_low}–{or_high})"]
    if rvol is not None:
        confidence += min(20, rvol * 5)
        reasons.append(f"OR RVOL {rvol}")
    if gap_aligned:
        confidence += 10
        reasons.append("gap aligned with break")
    if break_strength > 0.5:
        confidence += 10
        reasons.append(f"decisive break ({break_strength:.2f}× range)")
    confidence = int(min(95, confidence))

    risk = abs(live_price - stop)
    reward = abs(target - live_price)
    rr = round(reward / risk, 3) if risk > 0 else None

    return {
        'action': action, 'confidence': confidence, 'reasons': reasons,
        'skip_reasons': [], 'stop_loss': stop, 'target': target,
        'risk_reward_ratio': rr, 'archetype': 'ORB',
        'or_high': or_high, 'or_low': or_low,
        'break_strength': round(break_strength, 3),
    }
