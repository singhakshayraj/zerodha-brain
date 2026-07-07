"""REQ §5 steps 6–7: level filter + level-anchored stops/targets (levels.py),
plus the brain _level_context counterfactual helper. Pure — no DB/network."""
import os
from unittest.mock import MagicMock, patch

with patch.dict(os.environ, {
    'SUPABASE_URL': 'https://fake.supabase.co',
    'SUPABASE_SERVICE_KEY': 'fake-key',
}):
    with patch('supabase.create_client', return_value=MagicMock()):
        import database  # noqa

import config
import levels


PACK = {
    'pdh': 110, 'pdl': 95, 'pdc': 104,
    'weekly_high': 115, 'weekly_low': 90,
    'round_levels': [100, 105, 110],
}


def test_relevant_levels_flattens_sorts_dedups():
    lv = levels.relevant_levels(PACK)
    assert lv == [90, 95, 100, 104, 105, 110, 115]


def test_relevant_levels_empty_pack():
    assert levels.relevant_levels(None) == []
    assert levels.relevant_levels({}) == []


# --- level filter ---

def test_filter_blocks_wall_within_half_r_long():
    # entry 100, stop 98 → R=2; wall (round level) at 100? use entry 99.5 so
    # nearest above is 100 → 0.5 away = 0.25R < 0.5 → blocked
    lv = [95, 100, 110]
    res = levels.level_filter(99.5, 'UP', r_value=2.0, levels=lv, block_r=0.5)
    assert res['ok'] is False and res['blocking_level'] == 100


def test_filter_ok_when_wall_far():
    lv = [95, 110]
    res = levels.level_filter(100, 'UP', r_value=2.0, levels=lv, block_r=0.5)
    assert res['ok'] is True and res['blocking_level'] == 110


def test_filter_ok_when_no_wall_above():
    res = levels.level_filter(120, 'UP', r_value=2.0, levels=[90, 100])
    assert res['ok'] is True and res['blocking_level'] is None


def test_filter_short_uses_level_below():
    lv = [99.8, 90]
    res = levels.level_filter(100, 'DOWN', r_value=2.0, levels=lv, block_r=0.5)
    assert res['ok'] is False and res['blocking_level'] == 99.8


def test_filter_ok_without_r_value():
    res = levels.level_filter(100, 'UP', r_value=None, levels=[101])
    assert res['ok'] is True


# --- anchored stop/target ---

def test_anchored_long():
    # entry 103, support 100, resistance 110, atr 4, buffer 0.25 → buf 1
    a = levels.anchored_stop_target(103, 'UP', [90, 100, 110, 120], atr=4,
                                    buffer_frac=0.25, min_rr=1.0)
    assert a['stop'] == 99.0            # 100 - 1
    assert a['target'] == 110
    assert a['stop_level'] == 100 and a['target_level'] == 110
    # RR = (110-103)/(103-99) = 7/4 = 1.75
    assert a['rr'] == 1.75


def test_anchored_short():
    # entry 108, resistance 110 (stop side), support 100 (target), atr 4,
    # buf 1 → stop 111, risk 3, reward 8, RR 8/3 = 2.67
    a = levels.anchored_stop_target(108, 'DOWN', [90, 100, 110], atr=4,
                                    buffer_frac=0.25, min_rr=1.0)
    assert a['stop'] == 111.0           # 110 + 1
    assert a['target'] == 100
    assert a['rr'] == round(8 / 3, 3)


def test_anchored_none_when_rr_below_min():
    # tight resistance → low RR → None
    a = levels.anchored_stop_target(108, 'UP', [100, 110], atr=4,
                                    buffer_frac=0.25, min_rr=1.5)
    assert a is None


def test_anchored_none_when_missing_side():
    # nothing above entry → no target → None
    assert levels.anchored_stop_target(130, 'UP', [90, 100], atr=4) is None
    # nothing below entry → no support → None
    assert levels.anchored_stop_target(80, 'UP', [90, 100], atr=4) is None


def test_anchored_none_bad_direction():
    assert levels.anchored_stop_target(100, 'FLAT', [90, 110], atr=4) is None


# --- brain _level_context ---

def test_level_context_empty_pack_safe():
    import brain
    snap = brain._level_context(None, {'action': 'BUY', 'stop_loss': 98,
                                       'indicators': {'atr_14': 2}}, 100)
    assert snap == {'levels_count': 0}


def test_level_context_hold_has_no_filter():
    import brain
    snap = brain._level_context(PACK, {'action': 'HOLD', 'stop_loss': 98,
                                       'indicators': {}}, 100)
    assert snap['levels_count'] == 7
    assert 'filter' not in snap


def test_level_context_buy_populates_filter_and_anchor():
    import brain
    snap = brain._level_context(
        PACK, {'action': 'BUY', 'stop_loss': 100,
               'indicators': {'atr_14': 4}}, 103)
    assert 'filter' in snap and 'anchored' in snap


def test_level_context_never_raises_on_garbage():
    import brain
    snap = brain._level_context({'round_levels': 'notalist'},
                                {'action': 'BUY'}, 100)
    assert isinstance(snap, dict)
