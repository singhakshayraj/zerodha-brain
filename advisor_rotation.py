"""Rotation targeting + sizing for the portfolio advisor (SE4 split part 2,
P-06). Pure — config thresholds + scored universe in, candidate/sizing out; no
I/O, no orders. Re-exported by portfolio_advisor so callers/tests keep using
portfolio_advisor.<name>.
"""
import config


def find_rotation_candidate(exit_score: int, sector: str, scored: dict,
                            min_gap: int = None, min_target_score: int = None):
    """Best rotation target for a weak holding, or None. Gate (all three must
    hold — rotate into strength, never into least-bad):
      exit_score <= ROTATION_MAX_EXIT_SCORE   (the holding is genuinely weak)
      target score >= ROTATION_MIN_TARGET_SCORE (the target is genuinely strong)
      target - exit >= ROTATION_MIN_GAP         (the upgrade is wide, not noise)
    Same-sector candidates preferred; cross-sector only when none qualify."""
    min_gap = config.ROTATION_MIN_GAP if min_gap is None else min_gap
    min_target = (config.ROTATION_MIN_TARGET_SCORE
                  if min_target_score is None else min_target_score)
    if exit_score is None or exit_score > config.ROTATION_MAX_EXIT_SCORE:
        return None

    def qualifies(c):
        return (c['score'] >= min_target
                and c['score'] - exit_score >= min_gap)

    ranked = sorted(scored.values(), key=lambda c: c['score'], reverse=True)
    for reason, pool in (
            ('same_sector', [c for c in ranked
                             if sector and c.get('sector') == sector]),
            ('cross_sector', ranked)):
        for c in pool:
            if qualifies(c):
                return {'symbol': c['symbol'], 'score': c['score'],
                        'sector': c.get('sector'), 'reason': reason,
                        'last_close': c.get('last_close'),
                        'weekly': c.get('weekly')}
    return None


# Deploy slightly under the freed capital: Zerodha releases ~80% of CNC sell
# proceeds same-day, and the buy shouldn't assume a perfect fill price.
ROTATION_DEPLOY_FRACTION = 0.95


def size_rotation(verdict: str, qty: int, last_price: float,
                  target_price: float) -> dict:
    """Concrete rotation sizing: how many shares to sell, how much capital
    that frees, and how many target shares it buys. TRIM is a half-exit.
    Pure; empty dict when unsizeable (no qty/price)."""
    if not qty or not last_price:
        return {}
    sell_qty = qty if verdict != 'TRIM' else max(1, qty // 2)
    freed = round(sell_qty * last_price, 2)
    out = {'rotation_sell_qty': sell_qty, 'rotation_freed_inr': freed}
    if target_price and target_price > 0:
        out['rotation_buy_qty'] = int(freed * ROTATION_DEPLOY_FRACTION
                                      // target_price)
        out['rotation_buy_price'] = round(float(target_price), 2)
    return out


def rotation_entry_quality(target: dict, sizing: dict,
                           total_value: float) -> dict:
    """Entry-quality read on a rotation TARGET (FA4/P-09, DARK). A rotation is
    only good if the destination is a quality entry, not merely a higher score:
      - weekly_downtrend: the target's weekly structure is DOWN — buying a name
        whose higher timeframe is falling is a countertrend entry (would_block).
      - single_name_pct: what % of the portfolio this one rotation buy would
        become; over ROTATION_MAX_SINGLE_NAME_PCT is an over-concentration
        (would_resize, not block).
    Pure — returns the structured flags; the caller stashes them on
    indicators.rotation_entry_quality and only enforces when the flag is on.
    Correlated-cluster weight (the 3rd FA4 lever) is deferred: cluster
    membership isn't computed for non-held names yet."""
    weekly = target.get('weekly')
    weekly_downtrend = weekly == 'DOWN'
    buy_qty = sizing.get('rotation_buy_qty') or 0
    buy_price = sizing.get('rotation_buy_price') or 0
    buy_val = buy_qty * buy_price
    single_name_pct = (round(buy_val / total_value * 100, 1)
                       if total_value and buy_val else None)
    over_single_name = (single_name_pct is not None
                        and single_name_pct > config.ROTATION_MAX_SINGLE_NAME_PCT)
    return {
        'weekly_trend': weekly,
        'weekly_downtrend': weekly_downtrend,
        'single_name_pct': single_name_pct,
        'over_single_name_cap': over_single_name,
        'single_name_cap_pct': config.ROTATION_MAX_SINGLE_NAME_PCT,
        'would_block': weekly_downtrend,      # refuse countertrend entries
        'would_resize': over_single_name,     # trim the buy to the cap
    }


def apply_rotation_quality(row: dict, target: dict, sizing: dict,
                           total_value: float) -> bool:
    """Attach the dark entry-quality flags to `row.indicators` and, when
    ROTATION_QUALITY_ENABLED, refuse or resize the rotation IN PLACE (FA4/P-09).
    Returns True iff the rotation was refused — the caller should then skip the
    rest of the rotation-reason build for that row. Dark by default: computes +
    stashes + logs, changes nothing, returns False."""
    eq = rotation_entry_quality(target, sizing, total_value)
    if isinstance(row.get('indicators'), dict):
        row['indicators']['rotation_entry_quality'] = eq
    if eq['would_block'] or eq['would_resize']:
        print(f"[advisor.rotation.quality] {target['symbol']} "
              f"weekly={eq['weekly_trend']} single_name={eq['single_name_pct']}% "
              f"block={eq['would_block']} resize={eq['would_resize']} "
              f"(enforced={config.ROTATION_QUALITY_ENABLED})")
    if config.ROTATION_QUALITY_ENABLED and eq['would_block']:
        # Refuse a countertrend rotation: drop the target + sizing, keep the
        # underlying HOLD/SELL verdict on the name.
        for k in ('rotation_target_symbol', 'rotation_target_score',
                  'rotation_reason', 'rotation_sell_qty', 'rotation_freed_inr',
                  'rotation_buy_qty', 'rotation_buy_price'):
            row.pop(k, None)
        row['reasons'] = (row.get('reasons') or []) + [
            f"Rotation into {target['symbol']} refused — its weekly "
            f"trend is down (countertrend entry)."]
        return True
    if (config.ROTATION_QUALITY_ENABLED and eq['would_resize']
            and sizing.get('rotation_buy_price')):
        cap_val = total_value * config.ROTATION_MAX_SINGLE_NAME_PCT / 100
        capped_qty = int(cap_val // sizing['rotation_buy_price'])
        row['rotation_buy_qty'] = max(0, capped_qty)
        sizing['rotation_buy_qty'] = row['rotation_buy_qty']
    return False
