"""Unsettled (T+1) shares count as held."""
from unittest.mock import patch
from kite_client import KiteClient


def test_get_holdings_adds_t1_quantity_and_keeps_settled():
    k = KiteClient.__new__(KiteClient)
    raw = [{'tradingsymbol': 'INFY', 'quantity': 0, 't1_quantity': 10},
           {'tradingsymbol': 'ITC', 'quantity': 80, 't1_quantity': 0},
           {'tradingsymbol': 'X', 'quantity': 5}]
    with patch.object(KiteClient, '_get', return_value=raw):
        h = {x['tradingsymbol']: x for x in k.get_holdings()}
    assert (h['INFY']['quantity'], h['INFY']['settled_quantity']) == (10, 0)
    assert (h['ITC']['quantity'], h['X']['quantity'], h['X']['settled_quantity']) == (80, 5, 5)
