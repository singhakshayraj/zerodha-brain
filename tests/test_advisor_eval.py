"""Part C evaluation primitives (advisor_eval)."""
import advisor_eval as e


def test_fama_macbeth_counts_dates_not_observations():
    pv = {'d1': [1, 0, 1, 0], 'd2': [1, 1, 0, 0], 'd3': [1, 0, 0, 0]}
    m, t, n = e.fama_macbeth(pv)
    assert n == 3                                # real n = dates, not 12
    assert abs(m - (0.5 + 0.5 + 0.25) / 3) < 1e-9


def test_effective_n_below_nominal_when_clustered():
    # perfectly clustered within date -> n_eff far below n
    pv = {'d1': [1, 1, 1, 1], 'd2': [0, 0, 0, 0], 'd3': [1, 1, 1, 1]}
    n, neff, icc, deff = e.effective_n(pv)
    assert n == 12 and neff < n and icc > 0.5


def test_deflated_threshold_rises_with_trials():
    assert abs(e.deflated_threshold(1) - 1.96) < 0.01
    assert abs(e.deflated_threshold(50) - 3.29) < 0.05
    assert e.deflated_threshold(253) > 3.5


def test_brier_and_reliability():
    assert abs(e.brier_score([(0.5, 1), (0.5, 0)]) - 0.25) < 1e-9
    # perfect probabilities -> brier 0
    assert e.brier_score([(1.0, 1), (0.0, 0)]) == 0.0
    rel = e.reliability([(0.55, 1), (0.55, 0), (0.85, 1)])
    assert rel and all(len(row) == 5 for row in rel)


def test_block_bootstrap_ci_brackets_mean():
    lo, hi = e.block_bootstrap_ci([0, 1, 0, 1, 1, 0, 1, 0] * 10, block=8)
    assert lo <= 0.5 <= hi
