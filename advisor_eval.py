"""Correct-standard-error evaluation primitives (Part C).

The project's pooled t-stats treat correlated observations as independent.
These are the honest replacements, gathered in one place so every result can
be re-scored the same way:

  fama_macbeth   per-date statistic, then t across dates (the real n)
  effective_n    intra-date ICC -> design effect -> n_eff
  block_bootstrap_ci   CI that respects serial dependence (block ~ horizon)
  brier / reliability  calibration, separate from AUC's discrimination
  deflated_threshold   Harvey-Liu-Zhu multiplicity haircut on the z-bar

None of these predict returns; they only measure how sure we may be.
"""
import math
import random
import statistics


def fama_macbeth(per_date_values: dict):
    """per_date_values: {date: [obs,...]} of a per-observation numeric (e.g.
    1/0 correctness, or alpha). Returns (mean_across_dates, t, n_dates)."""
    dates = sorted(d for d, v in per_date_values.items() if v)
    means = [statistics.fmean(per_date_values[d]) for d in dates]
    n = len(means)
    if n < 2:
        return (means[0] if means else None), None, n
    m = statistics.fmean(means)
    sd = statistics.stdev(means)
    t = m / (sd / math.sqrt(n)) if sd > 0 else float('inf')
    return m, t, n


def effective_n(per_date_values: dict):
    """One-way ICC of a binary/continuous outcome clustered by date -> design
    effect 1+(m_bar-1)*ICC -> n_eff. Returns (n, n_eff, icc, deff)."""
    dates = [d for d, v in per_date_values.items() if v]
    groups = [per_date_values[d] for d in dates]
    n = sum(len(g) for g in groups)
    k = len(groups)
    if k < 2 or n <= k:
        return n, n, 0.0, 1.0
    grand = statistics.fmean([x for g in groups for x in g])
    ssb = sum(len(g) * (statistics.fmean(g) - grand) ** 2 for g in groups)
    ssw = sum(sum((x - statistics.fmean(g)) ** 2 for x in g) for g in groups)
    msb, msw = ssb / (k - 1), ssw / (n - k)
    n0 = (n - sum(len(g) ** 2 for g in groups) / n) / (k - 1)
    denom = msb + (n0 - 1) * msw
    icc = max(0.0, (msb - msw) / denom) if denom else 0.0
    m_bar = n / k
    deff = 1 + (m_bar - 1) * icc
    return n, n / deff, icc, deff


def block_bootstrap_ci(series: list, block: int, stat=statistics.fmean,
                       reps: int = 2000, alpha: float = 0.05, seed: int = 7):
    """Circular block bootstrap CI — respects serial dependence. `block` ~ the
    evaluation horizon in observations."""
    n = len(series)
    if n == 0:
        return (None, None)
    block = max(1, min(block, n))
    rng = random.Random(seed)
    ests = []
    nblocks = math.ceil(n / block)
    for _ in range(reps):
        sample = []
        for _ in range(nblocks):
            start = rng.randrange(n)
            sample += [series[(start + j) % n] for j in range(block)]
        ests.append(stat(sample[:n]))
    ests.sort()
    lo = ests[int((alpha / 2) * reps)]
    hi = ests[int((1 - alpha / 2) * reps) - 1]
    return (lo, hi)


def brier_score(pairs):
    """pairs: [(prob in [0,1], outcome 0/1)]. Lower = better; 0.25 = always 0.5."""
    ps = [(p, y) for p, y in pairs if p is not None]
    if not ps:
        return None
    return statistics.fmean((p - y) ** 2 for p, y in ps)


def reliability(pairs, bins=(0.5, 0.6, 0.7, 0.8, 0.9, 1.01)):
    """-> [(bin_lo, bin_hi, n, mean_pred, empirical_rate)] for a reliability
    diagram. Confidence expressed as a probability in [0,1]."""
    ps = [(p, y) for p, y in pairs if p is not None]
    out = []
    lo = 0.0
    for hi in bins:
        b = [(p, y) for p, y in ps if lo <= p < hi]
        if b:
            out.append((round(lo, 2), round(hi, 2), len(b),
                        statistics.fmean(p for p, _ in b),
                        statistics.fmean(y for _, y in b)))
        lo = hi
    return out


def deflated_threshold(n_trials: int, base_alpha: float = 0.05):
    """Harvey-Liu-Zhu style multiplicity haircut. Bonferroni control across
    n_trials independent tests -> per-test alpha = base_alpha/n_trials -> the
    two-sided z that must be cleared. At ~50 trials this is ~3.29, not 1.96."""
    from statistics import NormalDist
    n_trials = max(1, n_trials)
    per = base_alpha / n_trials
    z = NormalDist().inv_cdf(1 - per / 2)
    return z
