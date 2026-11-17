import math

from lab_stats import cumulative, day_row, sprt_bounds

_KEYS = ('n', 'wins', 'gross_inr', 'charges_inr', 'slippage_inr', 'net_inr', 'net_win_inr',
         'net_loss_inr', 'sum_gross_r', 'sum_cost_r', 'sum_net_r')


def _fill(days):
    return [{**{k: 0 for k in _KEYS}, **d} for d in days]


def _strong_days(t_high):
    sums = [400, 380, 410, 390] if t_high else [400, -150, 300, -150]
    return [{'n': 250, 'sum_net_r': s, 'net_inr': 500, 'net_win_inr': 2000, 'net_loss_inr': 1000,
             'wins': 120} for s in sums]


def test_sprt_bounds_n60():
    A, B = sprt_bounds(60)
    assert abs(A - math.log(0.8 / (0.05 / 60))) < 1e-9
    assert abs(B - math.log(0.2 / (1 - 0.05 / 60))) < 1e-9


def test_kill_line_matches_spec():          # S <= 0.05n - 36 at N=60
    days = [{'n': 1000, 'sum_net_r': 0.05 * 1000 - 37, 'net_inr': -1, 'net_win_inr': 1,
             'net_loss_inr': 2, 'wins': 400}]
    assert cumulative(_fill(days), 60)['sprt_state'] == 'killed'


def test_promote_needs_all_four_gates():
    c = cumulative(_fill(_strong_days(t_high=False)), 54)
    assert c['sprt_llr'] >= sprt_bounds(54)[0] and c['sprt_state'] == 'running'
    assert cumulative(_fill(_strong_days(t_high=True)), 54)['sprt_state'] == 'promoted'


def test_t_day_uses_daily_means_not_trades():
    c = cumulative(_fill([{'n': 100, 'sum_net_r': 10}, {'n': 1, 'sum_net_r': -1}]), 54)
    assert c['cum_days'] == 2 and c['cum_n'] == 101
    m = (0.1 - 1.0) / 2
    sd = math.sqrt(((0.1 - m) ** 2 + (-1.0 - m) ** 2) / 1)
    assert abs(c['t_day'] - m / (sd / math.sqrt(2))) < 1e-9


def test_pf_none_without_losses():
    assert cumulative(_fill([{'n': 1, 'net_win_inr': 50, 'net_loss_inr': 0}]), 54)['pf'] is None


def test_day_row_sums_and_splits_wins_losses():
    trades = [{'net_inr': 30.0, 'gross_inr': 50.0, 'charges_inr': 15.0, 'slippage_inr': 5.0,
               'gross_r': 1.0, 'cost_r': 0.4, 'net_r': 0.6},
              {'net_inr': -20.0, 'gross_inr': -5.0, 'charges_inr': 10.0, 'slippage_inr': 5.0,
               'gross_r': -0.1, 'cost_r': 0.3, 'net_r': -0.4}]
    r = day_row(trades)
    assert r['n'] == 2 and r['wins'] == 1 and r['net_inr'] == 10.0
    assert r['net_win_inr'] == 30.0 and r['net_loss_inr'] == 20.0
    assert abs(r['sum_net_r'] - 0.2) < 1e-9 and abs(r['sum_cost_r'] - 0.7) < 1e-9
