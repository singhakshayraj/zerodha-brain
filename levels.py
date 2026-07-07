"""Level filter + level-anchored stops/targets — ENGINEERING_SPEC §5 steps
6–7 (REQ pipeline). Pure functions over a level_pack row.

Idea: real intraday stops and targets belong AT structure, not at an ATR
multiple in empty space. Stop goes just beyond the nearest level on the risk
side (padded by a fraction of ATR so noise doesn't tag it); target is the
first opposing level; and an entry is rejected when a wall sits within 0.5R
in the profit direction (no room to run).

direction: 'UP' for longs, 'DOWN' for shorts.
"""

import config


def relevant_levels(pack: dict) -> list:
    """Flatten a level_pack row into a sorted, de-duplicated list of price
    levels the pipeline reasons about."""
    if not pack:
        return []
    out = []
    for k in ('pdh', 'pdl', 'pdc', 'weekly_high', 'weekly_low'):
        v = pack.get(k)
        if v:
            out.append(float(v))
    for lvl in (pack.get('round_levels') or []):
        if lvl:
            out.append(float(lvl))
    return sorted(set(out))


def _nearest_below(price: float, levels: list):
    below = [l for l in levels if l < price]
    return max(below) if below else None


def _nearest_above(price: float, levels: list):
    above = [l for l in levels if l > price]
    return min(above) if above else None


def level_filter(entry: float, direction: str, r_value: float, levels: list,
                 block_r: float = None) -> dict:
    """Reject entries that run straight into a wall: a level within block_r ×
    R in the PROFIT direction leaves no room. r_value is the ₹ risk per share
    (entry−stop distance).

    Returns {ok, blocking_level, distance_r}."""
    block_r = config.LEVEL_PROXIMITY_BLOCK_R if block_r is None else block_r
    if not levels or not r_value or r_value <= 0:
        return {'ok': True, 'blocking_level': None, 'distance_r': None}

    wall = _nearest_above(entry, levels) if direction == 'UP' else _nearest_below(entry, levels)
    if wall is None:
        return {'ok': True, 'blocking_level': None, 'distance_r': None}

    distance_r = abs(wall - entry) / r_value
    if distance_r < block_r:
        return {'ok': False, 'blocking_level': round(wall, 2),
                'distance_r': round(distance_r, 3)}
    return {'ok': True, 'blocking_level': round(wall, 2),
            'distance_r': round(distance_r, 3)}


def anchored_stop_target(entry: float, direction: str, levels: list,
                         atr: float, buffer_frac: float = None,
                         min_rr: float = None) -> dict:
    """Stop beyond the nearest level on the risk side (± buffer_frac × ATR);
    target = first opposing level. Returns None when structure is missing on
    either side or the resulting RR is below min_rr — the pipeline then falls
    back to the ATR stop rather than forcing a bad anchor.

    Returns {stop, target, rr, stop_level, target_level} or None.
    """
    buffer_frac = config.LEVEL_STOP_BUFFER_FRAC if buffer_frac is None else buffer_frac
    min_rr = config.MIN_RISK_REWARD_RATIO if min_rr is None else min_rr
    if not levels or not entry or entry <= 0:
        return None
    buf = (atr or 0) * buffer_frac

    support = _nearest_below(entry, levels)
    resistance = _nearest_above(entry, levels)
    if support is None or resistance is None:
        return None

    if direction == 'UP':
        stop_level, target_level = support, resistance   # stop below, target above
        stop = round(stop_level - buf, 2)
        target = round(target_level, 2)
        risk = entry - stop
        reward = target - entry
    elif direction == 'DOWN':
        stop_level, target_level = resistance, support   # stop above, target below
        stop = round(stop_level + buf, 2)
        target = round(target_level, 2)
        risk = stop - entry
        reward = entry - target
    else:
        return None

    if risk <= 0 or reward <= 0:
        return None
    rr = reward / risk
    if rr < min_rr:
        return None
    return {
        'stop': stop, 'target': target, 'rr': round(rr, 3),
        'stop_level': round(stop_level, 2), 'target_level': round(target_level, 2),
    }
