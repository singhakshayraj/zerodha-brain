"""TARGET_HIT fills are capped the same way STOP_LOSS_HIT fills are.

[P-05] capped the ~30s poll-latency tail on the STOP side in August and left
the TARGET side raw, so the identical artifact kept booking pullback as lost
gain: measured TARGET_HIT +1.389R against a planned 2.08R, with avg mfe_r
1.610R -- BELOW the target the trade must have touched to be classified
TARGET_HIT at all.

Capping losses but not gains biases every gate metric downward. These tests
pin the symmetry, and the last one pins the honesty constraint: the cap may
never invent a fill better than the target.
"""
import os
from unittest.mock import MagicMock, patch

with patch.dict(os.environ, {
    'SUPABASE_URL': 'https://fake.supabase.co',
    'SUPABASE_SERVICE_KEY': 'fake-key',
}):
    with patch('supabase.create_client', return_value=MagicMock()):
        import database  # noqa

import config
from brain import TradingBrain


def _brain():
    return TradingBrain.__new__(TradingBrain)


LONG = {'entry_price': 100.0, 'stop_loss_price': 95.0, 'target_price': 110.0}
SHORT = {'entry_price': 100.0, 'stop_loss_price': 105.0, 'target_price': 90.0}
# risk/share = 5.0, so the default 0.25 cap is a 1.25 band


def test_long_pullback_is_capped_to_the_band():
    """Poll caught 104 long after the trade touched 110 — book 108.75, the
    band edge, not the 30s-late price."""
    assert _brain()._target_fill_price(LONG, 104.0, False) == 108.75


def test_short_pullback_is_capped_to_the_band():
    assert _brain()._target_fill_price(SHORT, 96.0, True) == 91.25


def test_small_pullback_inside_the_band_is_kept():
    """Genuine slippage inside the band is real and must survive."""
    assert _brain()._target_fill_price(LONG, 109.5, False) == 109.5
    assert _brain()._target_fill_price(SHORT, 90.5, True) == 90.5


def test_never_fabricates_a_fill_better_than_target():
    """The honesty constraint. A poll above the long target must NOT book the
    extra — that would manufacture gain the cap has no basis to claim."""
    assert _brain()._target_fill_price(LONG, 115.0, False) == 110.0
    assert _brain()._target_fill_price(SHORT, 85.0, True) == 90.0


def test_disabled_cap_returns_the_raw_poll():
    original = config.PAPER_TARGET_SLIPPAGE_CAP_R
    config.PAPER_TARGET_SLIPPAGE_CAP_R = 0.0
    try:
        assert _brain()._target_fill_price(LONG, 104.0, False) == 104.0
    finally:
        config.PAPER_TARGET_SLIPPAGE_CAP_R = original


def test_degenerate_rows_fall_back_to_the_poll():
    assert _brain()._target_fill_price({}, 104.0, False) == 104.0
    bad = {'entry_price': 100.0, 'stop_loss_price': 100.0, 'target_price': 110.0}
    assert _brain()._target_fill_price(bad, 104.0, False) == 104.0


def test_both_capped_exits_skip_double_slippage():
    """paper_broker must not re-apply PAPER_SLIPPAGE_PCT on a pre-capped
    fill (the P-27 double-count). Both reasons now qualify."""
    assert 'STOP_LOSS_HIT' in TradingBrain.CAPPED_EXITS
    assert 'TARGET_HIT' in TradingBrain.CAPPED_EXITS
    assert 'EOD_CLOSE' not in TradingBrain.CAPPED_EXITS


def test_dispatcher_routes_each_reason():
    b = _brain()
    assert b._exit_fill_price(LONG, 104.0, False, 'TARGET_HIT') == 108.75
    assert b._exit_fill_price(LONG, 90.0, False, 'STOP_LOSS_HIT') == 93.75
    # anything else fills at the polled price, unchanged
    assert b._exit_fill_price(LONG, 104.0, False, 'EOD_CLOSE') == 104.0
