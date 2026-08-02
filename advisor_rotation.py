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
                        'last_close': c.get('last_close')}
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
