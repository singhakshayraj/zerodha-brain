"""market_context must persist the breadth it already computes.

_market_context() returns advancers/decliners every cycle and the columns exist,
but the payload omitted them -- so advancing_stocks/declining_stocks were NULL
on every row while the numbers sat in the dict.
"""
from unittest.mock import patch

import brain


def _payload():
    captured = {}
    b = brain.TradingBrain.__new__(brain.TradingBrain)
    b.session_id = 's1'
    b.last_context_log = None          # no interval guard on first call
    nifty = {'level': 0, 'change_percent': 0.25, 'direction': 'SIDEWAYS',
             'advancers': 31, 'decliners': 14, 'realized_vol': 0.4}
    with patch.object(brain.db, 'log_market_context',
                      side_effect=lambda sid, d: captured.update(d)):
        brain.TradingBrain._maybe_log_market_context(b, nifty, 'NORMAL')
    return captured


def test_breadth_is_persisted():
    p = _payload()
    assert p, 'payload never built -- guard path changed, test is now vacuous'
    assert p['advancing_stocks'] == 31
    assert p['declining_stocks'] == 14


def test_still_writes_what_it_always_did():
    p = _payload()
    assert p['nifty_change_percent'] == 0.25
    assert p['nifty_direction'] == 'SIDEWAYS'
    assert p['volatility_bucket'] == 'LOW'
