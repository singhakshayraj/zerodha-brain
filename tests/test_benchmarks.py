"""benchmarks.py — sector / size cohorts for call grading [P-68 phase 0]."""
import benchmarks as bm


def _bars(**closes):
    return {s: {'2026-11-10': a, '2026-11-12': b} for s, (a, b) in closes.items()}


def test_cohort_return_null_under_min_n():
    bars = _bars(A=(100, 110), B=(100, 90), C=(100, 100))
    assert bm.cohort_return(bars, ['A', 'B', 'C'], '2026-11-10', '2026-11-12') is None
    assert bm.cohort_return(bars, ['A', 'B', 'C'], '2026-11-10', '2026-11-12', min_n=3) == 0.0


def test_cohort_return_is_equal_weight_and_skips_unpriced():
    bars = _bars(A=(100, 110), B=(100, 90), C=(100, 104), D=(100, 102), E=(100, 100))
    bars['F'] = {'2026-11-10': 100}                       # no close on d1 -> skipped
    assert bm.cohort_return(bars, list('ABCDEF'), '2026-11-10', '2026-11-12') == 1.2


def test_size_buckets_default_micro_and_sectors_from_pin():
    sz = bm.size_buckets()
    assert sz.get('RELIANCE') == 'large' and sz.get('NOT-A-NAME', 'micro') == 'micro'
    assert bm.sectors().get('SBIN') == 'Financial Services'
