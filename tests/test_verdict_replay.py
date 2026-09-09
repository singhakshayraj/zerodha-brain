"""Part B replay grading rule — must match the production alpha label exactly."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from scripts.advisor_verdict_replay import _grade


def test_grade_matches_alpha_rule():
    # HOLD correct iff forward alpha > 0
    assert _grade('HOLD', 2.0) is True
    assert _grade('HOLD', -2.0) is False
    # exits correct iff forward alpha < 0 (name then underperformed)
    for v in ('SELL', 'SELL_ON_BOUNCE', 'TRIM'):
        assert _grade(v, -1.0) is True
        assert _grade(v, 1.0) is False
    # non-actionable verdicts are not graded
    assert _grade('INSUFFICIENT', 5.0) is None
    assert _grade('UNKNOWN', 5.0) is None
