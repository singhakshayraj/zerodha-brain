"""Part A covariance estimators (advisor_risk). Synthetic-data checks."""
import numpy as np
import advisor_risk as r


def _book(seed=1, n=250):
    rng = np.random.default_rng(seed)
    metal = rng.normal(0, 0.02, n)                 # one metal factor
    # 3 near-identical vehicles on the metal + 4 independent equities
    cols = [metal + rng.normal(0, 0.001, n) for _ in range(3)]
    cols += [rng.normal(0, 0.02, n) for _ in range(4)]
    return np.column_stack(cols)                    # (n, 7)


def test_ledoit_wolf_shrinks_and_conditions():
    R = _book()
    cov, shrink = r.ledoit_wolf_cov(R)
    assert 0.0 <= shrink <= 1.0
    assert cov.shape == (7, 7)
    assert np.all(np.linalg.eigvalsh(cov) > 0)      # positive definite


def test_effective_bets_collapse_on_redundant_metal():
    """3 vehicles on one metal + 4 independents -> far fewer than 7 bets."""
    cov, _ = r.ledoit_wolf_cov(_book())
    w = np.ones(7) / 7
    enb = r.effective_number_of_bets(cov, w)
    assert 1.0 <= enb < 6.0                          # not 7 independent bets
    # a book that is ALL three redundant vehicles -> close to 1 bet
    cov3, _ = r.ledoit_wolf_cov(_book()[:, :3])
    assert r.effective_number_of_bets(cov3, np.ones(3) / 3) < 1.6


def test_risk_share_exceeds_capital_share_for_correlated_block():
    cov, _ = r.ledoit_wolf_cov(_book())
    w = np.ones(7) / 7                               # equal capital
    rc = r.risk_contributions(cov, w)
    assert abs(rc['pctr'].sum() - 1.0) < 1e-9        # shares sum to 1
    metal_capital = 3 / 7
    metal_risk = rc['pctr'][:3].sum()
    assert metal_risk > metal_capital                # concentration hidden in capital


def test_hrp_downweights_the_redundant_cluster():
    cov, _ = r.ledoit_wolf_cov(_book())
    w = r.hierarchical_risk_parity(cov)
    assert abs(w.sum() - 1.0) < 1e-9
    # each redundant vehicle gets less than an equal 1/7 share
    assert w[:3].mean() < 1 / 7


def test_correlation_regime_flags_drift():
    rng = np.random.default_rng(3)
    n = 200
    base = rng.normal(0, 0.02, n)
    rest = rng.normal(0, 0.02, n)
    # sleeve decorrelated early, then tracks 'rest' in the last window
    sleeve = np.concatenate([rng.normal(0, 0.02, n - 60), rest[-60:] + rng.normal(0, 0.002, 60)])
    R = np.column_stack([sleeve, rest])
    out = r.sleeve_correlation_regime(R, np.array([True, False]), np.array([0.5, 0.5]), window=60)
    assert out['corr_recent'] > out['corr_full']     # recently more correlated
    assert out['drifted'] is True
