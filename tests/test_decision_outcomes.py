"""Track C (2026-07-15): counterfactual decision-outcome labeling.
label_one/_walk_forward/_r_multiple are pure functions — tested without
any DB mocking. label_decisions_for_date is the thin DB-driving wrapper."""
import os
from unittest.mock import MagicMock, patch

with patch.dict(os.environ, {
    'SUPABASE_URL': 'https://fake.supabase.co',
    'SUPABASE_SERVICE_KEY': 'fake-key',
}):
    with patch('supabase.create_client', return_value=MagicMock()):
        import database  # noqa

import decision_outcomes as do


def _bar(lo, hi, close=None):
    return {'low': lo, 'high': hi, 'close': close if close is not None else hi,
            'open': lo, 'ts': '2026-07-15T04:00:00+00:00'}


# --- _walk_forward ---

def test_walk_forward_long_stop_hit_first_bar():
    price, reason, bars = do._walk_forward(
        'LONG', 100, 95, 110, [_bar(94, 101)])
    assert (price, reason, bars) == (95, 'STOP_HIT', 1)


def test_walk_forward_long_target_hit():
    price, reason, bars = do._walk_forward(
        'LONG', 100, 95, 110, [_bar(99, 105), _bar(105, 111)])
    assert (price, reason, bars) == (110, 'TARGET_HIT', 2)


def test_walk_forward_short_stop_hit():
    # short: stop ABOVE entry, target BELOW entry
    price, reason, bars = do._walk_forward(
        'SHORT', 100, 105, 90, [_bar(99, 106)])
    assert (price, reason, bars) == (105, 'STOP_HIT', 1)


def test_walk_forward_short_target_hit():
    price, reason, bars = do._walk_forward(
        'SHORT', 100, 105, 90, [_bar(89, 99)])
    assert (price, reason, bars) == (90, 'TARGET_HIT', 1)


def test_walk_forward_same_bar_ambiguity_resolves_stop_first():
    # bar straddles BOTH stop and target — can't know intrabar order from
    # OHLC alone; conservative choice is stop-first, matching live priority.
    price, reason, bars = do._walk_forward(
        'LONG', 100, 95, 110, [_bar(94, 111)])
    assert reason == 'STOP_HIT'


def test_walk_forward_session_end_no_hit():
    price, reason, bars = do._walk_forward(
        'LONG', 100, 95, 110, [_bar(98, 102, close=101), _bar(99, 103, close=102)])
    assert (price, reason, bars) == (102, 'SESSION_END', 2)


def test_walk_forward_no_candles_is_no_data():
    price, reason, bars = do._walk_forward('LONG', 100, 95, 110, [])
    assert (price, reason, bars) == (None, 'NO_DATA', 0)


# --- _r_multiple ---

def test_r_multiple_long_win():
    assert do._r_multiple('LONG', 100, 95, 110) == 2.0   # risk 5, reward 10


def test_r_multiple_long_loss():
    assert do._r_multiple('LONG', 100, 95, 95) == -1.0


def test_r_multiple_short_win():
    assert do._r_multiple('SHORT', 100, 105, 90) == 2.0


def test_r_multiple_zero_risk_is_none():
    assert do._r_multiple('LONG', 100, 100, 110) is None


# --- label_one (pure, no DB except the candle fetch it makes) ---

def _decision(signal='BUY', price=100.0, stop=95.0, target=110.0):
    return {
        'id': 'dec-1', 'symbol': 'INFY', 'signal': signal,
        'price_at_decision': price,
        'indicators': {'stop_loss': stop, 'target': target},
        'created_at': '2026-07-15T04:00:00+00:00',
    }


def test_label_one_long_win():
    with patch.object(do.db, 'get_candles_for_symbol_from',
                      return_value=[_bar(99, 111)]):
        row = do.label_one(_decision())
    assert row['direction'] == 'LONG'
    assert row['exit_reason'] == 'TARGET_HIT'
    assert row['outcome'] == 'WIN'
    assert row['r_multiple'] == 2.0


def test_label_one_short_loss():
    with patch.object(do.db, 'get_candles_for_symbol_from',
                      return_value=[_bar(104, 106)]):
        row = do.label_one(_decision(signal='SELL', stop=105.0, target=90.0))
    assert row['direction'] == 'SHORT'
    assert row['exit_reason'] == 'STOP_HIT'
    assert row['outcome'] == 'LOSS'


def test_label_one_missing_stop_or_target_is_no_data():
    d = _decision()
    d['indicators'] = {'stop_loss': None, 'target': 110.0}
    row = do.label_one(d)
    assert row['exit_reason'] == 'NO_DATA'
    assert row['outcome'] is None


def test_label_one_no_candles_is_no_data():
    with patch.object(do.db, 'get_candles_for_symbol_from', return_value=[]):
        row = do.label_one(_decision())
    assert row['exit_reason'] == 'NO_DATA'
    assert row['bars_used'] == 0


def test_label_one_session_end_gets_unresolved_outcome():
    with patch.object(do.db, 'get_candles_for_symbol_from',
                       return_value=[_bar(99, 103, close=102)]):
        row = do.label_one(_decision())
    assert row['exit_reason'] == 'SESSION_END'
    assert row['outcome'] in ('UNRESOLVED', 'WIN')  # r=+0.4 -> WIN actually
    # 102 vs entry 100, stop 95: r = (102-100)/5 = 0.4 > 0 -> WIN, sanity check
    assert row['r_multiple'] == 0.4
    assert row['outcome'] == 'WIN'


# --- label_decisions_for_date (thin wrapper) ---

def test_label_decisions_for_date_stores_and_skips_failures():
    decisions = [_decision(), {'id': 'dec-2', 'symbol': 'BAD',
                                'signal': 'BUY', 'price_at_decision': 100.0,
                                'indicators': {}, 'created_at': '2026-07-15T04:00:00+00:00'}]
    with patch.object(do.db, 'get_directional_decisions_for_date',
                      return_value=decisions), \
         patch.object(do.db, 'get_candles_for_symbol_from',
                      return_value=[_bar(99, 111)]), \
         patch.object(do.db, 'insert_decision_outcome', return_value=True) as ins:
        n = do.label_decisions_for_date('2026-07-15')
    assert n == 2   # dec-2 has no stop/target -> NO_DATA row, still stored
    assert ins.call_count == 2


def test_label_decisions_for_date_survives_exception_in_one_row():
    decisions = [_decision(), _decision()]

    def _boom(*a, **k):
        raise RuntimeError('boom')

    with patch.object(do.db, 'get_directional_decisions_for_date',
                      return_value=decisions), \
         patch.object(do.db, 'get_candles_for_symbol_from', side_effect=_boom), \
         patch.object(do.db, 'insert_decision_outcome', return_value=True):
        n = do.label_decisions_for_date('2026-07-15')
    assert n == 0   # both failed the candle fetch, neither aborted the loop
