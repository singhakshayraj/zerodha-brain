"""P-05: STOP_LOSS_HIT fill-cap tests. The ~30s exit poll catches price after
it has drifted past the stop, booking the poll-latency tail as loss (measured
STOP_LOSS_HIT avg −1.62R vs the −1R the stop is sized for). _stop_fill_price
models a resting broker-side stop-market fill: capped a bounded band past the
stop, so only STOP_LOSS_HIT exits are trimmed, genuine slippage inside the band
is kept, and no fill is ever better than the stop."""
import os
import threading
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
    b = TradingBrain.__new__(TradingBrain)
    b.session_id = 's1'
    b.consecutive_losses = 0
    b._session_ended = False
    b._cycle_lock = threading.Lock()
    b._time_stop_logged = set()
    b._excursion = {}
    b.session_stats = {'trades_executed': 1, 'total_pnl': 0.0,
                       'winning_trades': 0, 'losing_trades': 0}
    return b


def _long(entry=100, stop=98, target=110):
    return {'id': 't1', 'symbol': 'INFY', 'exchange': 'NSE',
            'position_type': 'LONG', 'stop_loss_price': stop,
            'target_price': target, 'quantity': 10, 'entry_value': 1000,
            'entry_price': entry}


def _short(entry=100, stop=102, target=90):
    return {'id': 't2', 'symbol': 'INFY', 'exchange': 'NSE',
            'position_type': 'SHORT', 'stop_loss_price': stop,
            'target_price': target, 'quantity': 10, 'entry_value': 1000,
            'entry_price': entry}


# --- the helper, in isolation ---

def test_long_blowthrough_capped_to_floor():
    # entry 100 / stop 98 → risk 2 · cap 0.25R → band 0.5 → floor 97.5.
    # polled price 95 blew 1.5 past the stop; capped up to the floor.
    b = _brain()
    with patch.object(config, 'PAPER_STOP_SLIPPAGE_CAP_R', 0.25):
        assert b._stop_fill_price(_long(), 95.0, False) == 97.5


def test_long_slippage_inside_band_kept_honest():
    # polled 97.7 is within the 97.5 floor → genuine slippage, not trimmed.
    b = _brain()
    with patch.object(config, 'PAPER_STOP_SLIPPAGE_CAP_R', 0.25):
        assert b._stop_fill_price(_long(), 97.7, False) == 97.7


def test_long_never_better_than_stop():
    # a fill above the stop would be fabricated favourably; must not happen.
    b = _brain()
    with patch.object(config, 'PAPER_STOP_SLIPPAGE_CAP_R', 0.25):
        px = b._stop_fill_price(_long(), 97.9, False)  # current between floor & stop
        assert px <= 98.0


def test_short_blowthrough_capped_to_ceiling():
    # entry 100 / stop 102 → risk 2 · band 0.5 → ceiling 102.5.
    # polled 105 blew past; capped down to ceiling.
    b = _brain()
    with patch.object(config, 'PAPER_STOP_SLIPPAGE_CAP_R', 0.25):
        assert b._stop_fill_price(_short(), 105.0, True) == 102.5


def test_cap_disabled_returns_raw_price():
    b = _brain()
    with patch.object(config, 'PAPER_STOP_SLIPPAGE_CAP_R', 0.0):
        assert b._stop_fill_price(_long(), 95.0, False) == 95.0


def test_degenerate_stop_returns_raw_price():
    b = _brain()
    with patch.object(config, 'PAPER_STOP_SLIPPAGE_CAP_R', 0.25):
        assert b._stop_fill_price(_long(entry=100, stop=100), 95.0, False) == 95.0


# --- integration through _evaluate_exit: BOTH capped exits ---
# Until 2026-08-27 only STOP_LOSS_HIT was capped and TARGET_HIT filled at the
# raw poll. That was the scope of [P-05], not a decision that targets should
# stay raw -- and capping the loss tail while leaving the gain tail biased
# every gate metric downward. See config.PAPER_TARGET_SLIPPAGE_CAP_R.

def test_evaluate_exit_caps_both_stop_and_target():
    b = _brain()
    with patch.object(config, 'PAPER_STOP_SLIPPAGE_CAP_R', 0.25), \
         patch.object(b, '_update_excursion'), \
         patch.object(b, '_execute_sell_by_trade') as sell:
        # long, price 95 below stop 98 → STOP_LOSS_HIT, fill capped to 97.5
        b._evaluate_exit(_long(), 95.0)
        assert sell.call_args[0][1] == 97.5
        assert sell.call_args[0][2] == 'STOP_LOSS_HIT'

    b2 = _brain()
    with patch.object(config, 'PAPER_STOP_SLIPPAGE_CAP_R', 0.25), \
         patch.object(b2, '_update_excursion'), \
         patch.object(b2, '_execute_sell_by_trade') as sell2:
        # long, price 111 above target 110 → TARGET_HIT. Now capped AT the
        # target: a resting target-limit fills at its limit, so the 1.00 the
        # poll happened to overshoot by is not ours to book. The cap trims the
        # favourable tail as well as the adverse one -- it is a fill model,
        # not a thumb on the scale.
        b2._evaluate_exit(_long(), 111.0)
        assert sell2.call_args[0][1] == 110.0
        assert sell2.call_args[0][2] == 'TARGET_HIT'


def test_evaluate_exit_caps_short_stop():
    b = _brain()
    with patch.object(config, 'PAPER_STOP_SLIPPAGE_CAP_R', 0.25), \
         patch.object(b, '_update_excursion'), \
         patch.object(b, '_cover_short') as cover:
        # short, price 105 above stop 102 → STOP_LOSS_HIT, fill capped to 102.5
        b._evaluate_exit(_short(), 105.0)
        assert cover.call_args[0][1] == 102.5


# --- P-27: short exits must carry the real reason, not a blanket COVER_SHORT ---

def test_evaluate_exit_passes_reason_through_for_shorts():
    """Until 08-06 every short exit was written as 'COVER_SHORT', so
    STOP_LOSS_HIT / TARGET_HIT / TIME_STOP measured LONGs only — the [P-05]
    verdict ('−1.252R, on target') described half the book."""
    for price, expected in ((105.0, 'STOP_LOSS_HIT'), (89.0, 'TARGET_HIT')):
        b = _brain()
        with patch.object(config, 'PAPER_STOP_SLIPPAGE_CAP_R', 0.25), \
             patch.object(b, '_update_excursion'), \
             patch.object(b, '_cover_short') as cover:
            b._evaluate_exit(_short(), price)
            assert cover.call_args.kwargs['exit_reason'] == expected


def test_cover_short_writes_the_given_reason():
    b = _brain()
    b.kite = MagicMock()
    b.order_manager = MagicMock()
    b.order_manager.cover_short_order.return_value = {
        'order_id': 'o1', 'price': 102.5, 'quantity': 10, 'value': 1025.0,
        'reference_price': 102.5, 'slippage_bps': 0.0, 'charges_bps': 0.0,
        'model_stop': True,
    }
    with patch.object(b, '_excursion_fields', return_value={}), \
         patch.object(b, '_exit_state', return_value={}), \
         patch.object(b, '_record_close_outcome'), \
         patch('brain.db.close_trade') as close, \
         patch('brain.db.update_stock_score'), \
         patch('brain.db.log_brain_activity'):
        b._cover_short(_short(), 102.5, is_stop=True,
                       exit_reason='STOP_LOSS_HIT')
        payload = close.call_args[0][1]
        assert payload['exit_reason'] == 'STOP_LOSS_HIT'
        # and the model_stop flag survives into the persisted execution blob
        assert payload['execution']['exit']['model_stop'] is True
