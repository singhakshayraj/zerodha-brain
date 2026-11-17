"""Strategy-lab statistics [P-70]: per-day rollup, PF, day-clustered t, SPRT.

Pre-registered in the design spec §5. The day, not the trade, is the unit of
independence (54 variants trade the same names at the same moments), so t is
computed on per-day mean net R. SPRT: H0 mu=0 vs H1 mu=0.1R, sigma=1.5R,
alpha=0.05/N, beta=0.2.
"""
import math

MU1, SIGMA, BETA, ALPHA_FAMILY = 0.1, 1.5, 0.2, 0.05
PROMOTE_T, PROMOTE_PF, PROMOTE_INR_DAY = 3.72, 1.3, 100.0


def sprt_bounds(n_variants: int) -> tuple:
    a = ALPHA_FAMILY / n_variants
    return math.log((1 - BETA) / a), math.log(BETA / (1 - a))


def day_row(trades: list) -> dict:
    nets = [t['net_inr'] for t in trades]
    return {'n': len(trades),
            'wins': sum(1 for x in nets if x > 0),
            'gross_inr': round(sum(t['gross_inr'] for t in trades), 2),
            'charges_inr': round(sum(t['charges_inr'] for t in trades), 2),
            'slippage_inr': round(sum(t['slippage_inr'] for t in trades), 2),
            'net_inr': round(sum(nets), 2),
            'net_win_inr': round(sum(x for x in nets if x > 0), 2),
            'net_loss_inr': round(-sum(x for x in nets if x < 0), 2),
            'sum_gross_r': sum(t['gross_r'] for t in trades),
            'sum_cost_r': sum(t['cost_r'] for t in trades),
            'sum_net_r': sum(t['net_r'] for t in trades)}


def cumulative(days: list, n_variants: int) -> dict:
    days = [d for d in days if d['n'] > 0]
    n = sum(d['n'] for d in days)
    s = sum(d['sum_net_r'] for d in days)
    loss = sum(d['net_loss_inr'] for d in days)
    net = sum(d['net_inr'] for d in days)
    means = [d['sum_net_r'] / d['n'] for d in days]
    t_day = None
    if len(means) >= 2:
        m = sum(means) / len(means)
        sd = math.sqrt(sum((x - m) ** 2 for x in means) / (len(means) - 1))
        t_day = m / (sd / math.sqrt(len(means))) if sd > 0 else None
    pf = sum(d['net_win_inr'] for d in days) / loss if loss > 0 else None
    mean_day = net / len(days) if days else 0.0
    llr = (MU1 / SIGMA ** 2) * s - n * MU1 ** 2 / (2 * SIGMA ** 2)
    A, B = sprt_bounds(n_variants)
    state = 'running'
    if llr <= B:
        state = 'killed'
    elif (llr >= A and t_day is not None and t_day >= PROMOTE_T and pf is not None
          and pf >= PROMOTE_PF and mean_day >= PROMOTE_INR_DAY):
        state = 'promoted'
    return {'cum_n': n, 'cum_days': len(days), 'cum_net_inr': round(net, 2), 'cum_net_r': s,
            'pf': pf, 'win_rate': (sum(d['wins'] for d in days) / n) if n else None,
            'mean_net_inr_day': round(mean_day, 2), 't_day': t_day,
            'sprt_llr': llr, 'sprt_state': state}
