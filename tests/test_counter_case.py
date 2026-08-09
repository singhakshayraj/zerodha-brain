"""[P-33] every verdict carries a bear case.

`reasons` is confirmatory by construction — it explains why the call is right.
Nothing recorded what would make it WRONG, so nothing could be scored against
it later. These tests pin the property that makes the verdict falsifiable:
a non-empty counter-case for every real verdict, naming a level where one is
known.
"""
import pytest

from advisor_scoring import build_counter_case

REAL_VERDICTS = ['HOLD', 'TRIM', 'SELL', 'SELL_ON_BOUNCE']


@pytest.mark.parametrize('verdict', REAL_VERDICTS)
def test_every_real_verdict_gets_a_counter_case(verdict):
    out = build_counter_case(verdict, 30, 100.0, support=95.0,
                             resistance=110.0, ema_21=98.0, rsi=55)
    assert out and out.endswith('.')


@pytest.mark.parametrize('verdict', REAL_VERDICTS)
def test_counter_case_survives_missing_levels(verdict):
    """Indicators are routinely absent (thin history, fresh listing). A missing
    level must degrade the sentence, never blank it or print 'None'."""
    out = build_counter_case(verdict, 25, 100.0)
    assert out and 'None' not in out and '₹' not in out


def test_insufficient_has_none():
    """Nothing to argue against — and the empty string is what lets a caller
    assert 'every non-INSUFFICIENT row has one'."""
    assert build_counter_case('INSUFFICIENT', 0, 100.0, support=95.0) == ''


def test_hold_names_the_level_that_breaks_it():
    out = build_counter_case('HOLD', 55, 100.0, support=95.0)
    assert '₹95.00' in out and 'daily close' in out


def test_hold_without_support_says_so_rather_than_inventing_one():
    """A counter-case that invents a number is worse than a vague one."""
    out = build_counter_case('HOLD', 55, 100.0, support=None)
    assert '₹' not in out
    assert 'no swing support' in out


def test_hold_flags_a_weekly_conflict():
    out = build_counter_case('HOLD', 55, 100.0, support=95.0, alignment='CONFLICT')
    assert 'weekly' in out.lower()


def test_hold_flags_a_thin_score():
    thin = build_counter_case('HOLD', 22, 100.0, support=95.0)
    strong = build_counter_case('HOLD', 80, 100.0, support=95.0)
    assert '+22' in thin
    assert '+80' not in strong        # not worth flagging a genuinely strong trend


def test_hold_flags_overbought():
    out = build_counter_case('HOLD', 55, 100.0, support=95.0, rsi=78)
    assert 'overbought' in out


def test_overextended_trim_argues_the_upside_not_the_downside():
    """The bear case for a TRIM is that you sold too early, not too late."""
    out = build_counter_case('TRIM', 60, 100.0, ema_21=97.0, overextended=True)
    assert 'caps the upside' in out and '₹97.00' in out


def test_mixed_trim_differs_from_overextended_trim():
    """Same verdict string, two different reasons for it — the counter-cases
    must not collapse into one."""
    a = build_counter_case('TRIM', 60, 100.0, ema_21=97.0, overextended=True)
    b = build_counter_case('TRIM', 5, 100.0, support=95.0, overextended=False)
    assert a != b
    assert 'Mixed structure' in b


def test_sell_argues_the_reversal():
    out = build_counter_case('SELL', -55, 100.0, ema_21=104.0, rsi=25)
    assert 'reversals start' in out
    assert '₹104.00' in out and 'oversold' in out


def test_sell_on_bounce_names_both_ways_it_fails():
    out = build_counter_case('SELL_ON_BOUNCE', -30, 100.0,
                             support=95.0, resistance=112.0)
    assert '₹95.00' in out and '₹112.00' in out


def test_unknown_verdict_is_loud_not_silent():
    """A new verdict added without a counter-case must announce itself rather
    than quietly return '' and look compliant."""
    out = build_counter_case('SOMETHING_NEW', 10, 100.0)
    assert 'unfalsifiable' in out


def test_advise_attaches_it_to_the_row():
    """The dict is spread straight into the portfolio_advice insert, so the key
    must be present on the real path, not just from the helper."""
    import advisor_scoring
    holding = {'tradingsymbol': 'X', 'quantity': 10,
               'average_price': 100.0, 'last_price': 101.0}
    out = advisor_scoring.advise(holding, [])          # forces INSUFFICIENT
    assert 'counter_case' in out
    assert out['verdict'] == 'INSUFFICIENT' and out['counter_case'] == ''
