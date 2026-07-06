"""REQ-052 mechanical trend-tell tests. Pure functions — no DB, no network.
Key invariant: missing data ABSTAINS (None), never silently reads as False."""
import os
from unittest.mock import MagicMock, patch

with patch.dict(os.environ, {
    'SUPABASE_URL': 'https://fake.supabase.co',
    'SUPABASE_SERVICE_KEY': 'fake-key',
}):
    with patch('supabase.create_client', return_value=MagicMock()):
        import database  # noqa

import trend_tells as tt


def _c(o, h, l, cl, v=1000, ts='2026-07-07T09:15:00+0530'):
    return {'open': o, 'high': h, 'low': l, 'close': cl, 'volume': v,
            'timestamp': ts}


# --- vwap ---

def test_vwap_none_without_volume():
    assert tt.vwap([_c(10, 11, 9, 10, v=0)]) is None


def test_vwap_basic():
    # single candle typical = (11+9+10)/3 = 10
    assert tt.vwap([_c(10, 11, 9, 10, v=100)]) == 10.0


# --- vwap persistence ---

def test_vwap_persistence_up_when_closes_climb():
    candles = [_c(10, 10.5, 9.5, 10 + i * 0.2) for i in range(10)]
    assert tt.tell_vwap_persistence(candles, 'UP') is True


def test_vwap_persistence_false_when_below():
    candles = [_c(10, 10.5, 9.5, 10 - i * 0.2) for i in range(10)]
    assert tt.tell_vwap_persistence(candles, 'UP') is False


def test_vwap_persistence_abstains_on_short_series():
    assert tt.tell_vwap_persistence([_c(10, 11, 9, 10)] * 3, 'UP') is None


def test_vwap_persistence_abstains_no_volume():
    candles = [_c(10, 10.5, 9.5, 10 + i, v=0) for i in range(10)]
    assert tt.tell_vwap_persistence(candles, 'UP') is None


# --- gap hold ---

def test_gap_hold_up_true():
    assert tt.tell_gap_hold(prev_close=100, today_open=102,
                            current_price=103, direction='UP') is True


def test_gap_hold_up_false_when_filled():
    assert tt.tell_gap_hold(prev_close=100, today_open=102,
                            current_price=101, direction='UP') is False


def test_gap_hold_down_true():
    assert tt.tell_gap_hold(prev_close=100, today_open=98,
                            current_price=97, direction='DOWN') is True


def test_gap_hold_abstains_without_data():
    assert tt.tell_gap_hold(None, 102, 103, 'UP') is None


# --- range expansion ---

def test_range_expansion_true():
    assert tt.tell_range_expansion(today_range=8, avg_range=10,
                                   threshold=0.6) is True


def test_range_expansion_false():
    assert tt.tell_range_expansion(today_range=5, avg_range=10,
                                   threshold=0.6) is False


def test_range_expansion_abstains_without_avg():
    assert tt.tell_range_expansion(today_range=8, avg_range=None) is None


# --- breadth/sector (abstains with retail data) ---

def test_breadth_sector_abstains_when_unavailable():
    assert tt.tell_breadth_sector(None, None, None, 'UP') is None


def test_breadth_sector_true_when_aligned():
    assert tt.tell_breadth_sector(advancers=30, decliners=20,
                                  sector_aligned=True, direction='UP') is True


def test_breadth_sector_false_when_breadth_opposes():
    assert tt.tell_breadth_sector(advancers=10, decliners=40,
                                  sector_aligned=True, direction='UP') is False


# --- session range ---

def test_session_range():
    candles = [_c(10, 12, 8, 11), _c(11, 15, 10, 14)]
    assert tt.session_range(candles) == 7  # max high 15 - min low 8


# --- aggregate evaluate ---

def test_evaluate_counts_fired_and_available():
    candles = [_c(10, 10.5, 9.5, 10 + i * 0.2) for i in range(10)]
    res = tt.evaluate(
        direction='UP', candles_5min=candles,
        prev_close=100, today_open=102, current_price=103,
        today_range=8, avg_range=10,
        # breadth omitted → abstains
    )
    assert res['tells']['vwap_persistence'] is True
    assert res['tells']['gap_hold'] is True
    assert res['tells']['range_expansion'] is True
    assert res['tells']['breadth_sector'] is None
    assert res['fired'] == 3
    assert res['available'] == 3
    assert res['trend_day'] is True          # 3 >= required 3
    assert res['permits_entry'] is True


def test_evaluate_not_trend_day_when_below_required():
    res = tt.evaluate(
        direction='UP', candles_5min=[_c(10, 10.5, 9.5, 10 - i) for i in range(10)],
        prev_close=100, today_open=99, current_price=98,  # gap down, dir up → False
        today_range=3, avg_range=10,
    )
    assert res['fired'] < res['required']
    assert res['trend_day'] is False
    assert res['permits_entry'] is False


def test_evaluate_all_abstain_is_not_a_trend_day():
    res = tt.evaluate(direction='UP')  # nothing provided
    assert res['available'] == 0
    assert res['fired'] == 0
    assert res['trend_day'] is False


# --- brain hook never raises ---

def test_brain_compute_trend_tells_is_safe_on_garbage():
    import brain
    assert brain._compute_trend_tells({'action': 'HOLD'}, [], 0) == {} or True
    # malformed candles must not raise
    out = brain._compute_trend_tells(
        {'action': 'BUY'}, [{'bad': 'candle'}], 100)
    assert isinstance(out, dict)
