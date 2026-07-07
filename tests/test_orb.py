"""REQ §5 step 5A — opening-range breakout archetype (orb.py)."""
import os
from unittest.mock import MagicMock, patch

with patch.dict(os.environ, {
    'SUPABASE_URL': 'https://fake.supabase.co',
    'SUPABASE_SERVICE_KEY': 'fake-key',
}):
    with patch('supabase.create_client', return_value=MagicMock()):
        import database  # noqa

import orb


def _or(high=110, low=100, rvol=3.0, gap=1.0):
    return {'or_high': high, 'or_low': low, 'or_rvol': rvol, 'gap_pct': gap}


# --- HOLD paths ---

def test_hold_inside_range():
    s = orb.orb_signal(105, _or())
    assert s['action'] == 'HOLD'
    assert 'inside opening range' in s['skip_reasons'][0].lower()


def test_hold_without_or_data():
    assert orb.orb_signal(105, None)['action'] == 'HOLD'
    assert orb.orb_signal(105, {})['action'] == 'HOLD'


def test_hold_degenerate_range():
    assert orb.orb_signal(105, _or(high=100, low=100))['action'] == 'HOLD'


def test_hold_no_price():
    assert orb.orb_signal(0, _or())['action'] == 'HOLD'


# --- breakouts ---

def test_break_above_is_buy():
    s = orb.orb_signal(112, _or(), break_buffer_frac=0.05)  # 110 + 0.5 buf
    assert s['action'] == 'BUY'
    assert s['stop_loss'] == 100          # far side of OR
    assert s['target'] == 120             # or_high + range
    assert s['archetype'] == 'ORB'
    assert s['risk_reward_ratio'] is not None


def test_break_below_is_sell():
    s = orb.orb_signal(98, _or(gap=-1.0), break_buffer_frac=0.05)
    assert s['action'] == 'SELL'
    assert s['stop_loss'] == 110          # far side (OR high)
    assert s['target'] == 90              # or_low - range


def test_just_inside_buffer_holds():
    # 110 + 0.05*10 = 110.5 threshold; 110.4 stays HOLD
    assert orb.orb_signal(110.4, _or(), break_buffer_frac=0.05)['action'] == 'HOLD'


# --- confidence model ---

def test_confidence_rewards_rvol_gap_and_decisive_break():
    strong = orb.orb_signal(118, _or(rvol=4.0, gap=1.5))   # decisive, aligned, high rvol
    weak = orb.orb_signal(111, _or(rvol=0.0, gap=-1.0))    # marginal, misaligned
    assert strong['confidence'] > weak['confidence']
    assert strong['confidence'] <= 95


def test_confidence_handles_missing_rvol_gap():
    s = orb.orb_signal(112, {'or_high': 110, 'or_low': 100})
    assert s['action'] == 'BUY'
    assert 0 < s['confidence'] <= 95


def test_gap_alignment_only_helps_matching_direction():
    # gap up should NOT boost a short
    short_gap_up = orb.orb_signal(98, _or(gap=2.0, rvol=0.0))
    short_gap_down = orb.orb_signal(98, _or(gap=-2.0, rvol=0.0))
    assert short_gap_down['confidence'] > short_gap_up['confidence']


# --- promotion actually clears the downstream entry gate ---

def test_promoted_orb_long_clears_buy_gate():
    """A promoted ORB long must carry confidence >= MIN_BUY_CONFIDENCE, or
    the BUY branch silently drops it."""
    import config
    s = orb.orb_signal(118, _or(rvol=4.0, gap=1.5))
    assert s['action'] == 'BUY'
    # confidence must be able to reach the BUY gate for a strong break
    assert s['confidence'] >= config.MIN_BUY_CONFIDENCE


def test_orb_min_confidence_matches_buy_gate():
    import config
    assert config.ORB_MIN_CONFIDENCE >= config.MIN_BUY_CONFIDENCE
