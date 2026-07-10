"""Slippage-decomposition persistence (Tier-2): the execution jsonb block —
entry leg written at open, exit leg merged at close without losing the entry."""
import os
from unittest.mock import MagicMock, patch

with patch.dict(os.environ, {
    'SUPABASE_URL': 'https://fake.supabase.co',
    'SUPABASE_SERVICE_KEY': 'fake-key',
}):
    with patch('supabase.create_client', return_value=MagicMock()):
        import database  # noqa

from brain import TradingBrain


def _fill(ref=100.0, price=100.5, bps=50.0):
    return {'order_id': 'PAPER-1', 'price': price, 'quantity': 10,
            'value': price * 10, 'reference_price': ref, 'slippage_bps': bps}


def test_execution_entry_block():
    b = TradingBrain.__new__(TradingBrain)
    block = b._execution_entry(_fill())
    assert block == {'entry': {'reference_price': 100.0,
                               'fill_price': 100.5, 'slippage_bps': 50.0}}


def test_execution_entry_none_for_real_broker_fill():
    # real OrderManager fills have no reference_price → no decomposition
    b = TradingBrain.__new__(TradingBrain)
    assert b._execution_entry({'price': 100.5, 'order_id': 'x'}) is None


def test_execution_exit_preserves_entry_leg():
    b = TradingBrain.__new__(TradingBrain)
    trade = {'execution': {'entry': {'reference_price': 100.0,
                                     'fill_price': 100.5, 'slippage_bps': 50.0}}}
    block = b._execution_exit(trade, _fill(ref=99.0, price=98.5, bps=50.5))
    assert block['entry']['reference_price'] == 100.0        # kept
    assert block['exit'] == {'reference_price': 99.0,
                             'fill_price': 98.5, 'slippage_bps': 50.5}


def test_execution_exit_none_without_decomposition():
    b = TradingBrain.__new__(TradingBrain)
    assert b._execution_exit({'execution': {}}, {'price': 1, 'order_id': 'x'}) is None
