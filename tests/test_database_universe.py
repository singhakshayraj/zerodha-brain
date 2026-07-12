"""Nifty 500 universe DB functions + config pin + seeding mapping (Phase 1
of the rotation-advisor build). All supabase calls mocked."""
import os
import sys
from unittest.mock import MagicMock, patch

with patch.dict(os.environ, {
    'SUPABASE_URL': 'https://fake.supabase.co',
    'SUPABASE_SERVICE_KEY': 'fake-key',
}):
    with patch('supabase.create_client', return_value=MagicMock()):
        import database

import config

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))
from seed_nifty500_universe import universe_rows  # noqa: E402
from build_nifty500_tokens import build_rows      # noqa: E402


# --- config pin -------------------------------------------------------------

def test_nifty500_pin_loaded():
    assert len(config.NIFTY500_UNIVERSE) == 500
    assert len(config.NIFTY500_INSTRUMENT_TOKENS) == 500
    # spot-check against the independently hand-pinned NIFTY50 dict
    assert (config.NIFTY500_INSTRUMENT_TOKENS['NSE:RELIANCE']
            == config.NIFTY50_INSTRUMENT_TOKENS['NSE:RELIANCE'])
    sample = config.NIFTY500_UNIVERSE[0]
    assert sample['symbol'] and sample['instrument_token'] > 0
    assert sample['sector']


# --- token-builder join (pure) ----------------------------------------------

def test_build_rows_joins_and_reports_missing():
    constituents = [
        {'Company Name': 'A Ltd', 'Industry': 'IT', 'Symbol': 'AAA'},
        {'Company Name': 'B Ltd', 'Industry': 'Banks', 'Symbol': 'BBB'},
        {'Company Name': 'C Ltd', 'Industry': 'Autos', 'Symbol': 'CCC'},
    ]
    instruments = [
        {'segment': 'NSE', 'instrument_type': 'EQ', 'tradingsymbol': 'AAA',
         'instrument_token': '111'},
        {'segment': 'NSE', 'instrument_type': 'EQ', 'tradingsymbol': 'BBB',
         'instrument_token': '222'},
        # CCC missing; a non-EQ AAA row must not shadow the EQ one
        {'segment': 'NFO-FUT', 'instrument_type': 'FUT', 'tradingsymbol': 'CCC',
         'instrument_token': '999'},
    ]
    rows, missing = build_rows(constituents, instruments)
    assert [r['symbol'] for r in rows] == ['AAA', 'BBB']
    assert rows[0]['instrument_token'] == 111
    assert rows[0]['sector'] == 'IT'
    assert missing == ['CCC']


# --- seeding mapping (pure) ---------------------------------------------------

def test_universe_rows_shape_never_touches_engine_columns():
    rows = universe_rows()
    assert len(rows) == 500
    r = rows[0]
    assert r['is_nifty500'] is True and r['is_active'] is True
    assert r['exchange'] == 'NSE'
    # the seed must never write paper-engine-owned columns
    forbidden = {'brain_score', 'total_trades', 'winning_trades', 'total_pnl',
                 'win_rate', 'avg_pnl_per_trade'}
    assert not (set(r) & forbidden)


# --- database functions -------------------------------------------------------

def test_get_stock_universe_nifty500_filter():
    with patch('database.supabase') as sb:
        q = sb.table.return_value.select.return_value.eq.return_value
        q.eq.return_value.order.return_value.execute.return_value.data = [
            {'symbol': 'AAA'}]
        out = database.get_stock_universe('NIFTY500')
    assert out == [{'symbol': 'AAA'}]
    q.eq.assert_called_once_with('is_nifty500', True)


def test_upsert_stock_universe_bulk_on_symbol_and_empty_noop():
    with patch('database.supabase') as sb:
        n = database.upsert_stock_universe_bulk([{'symbol': 'AAA'}])
        assert n == 1
        sb.table.return_value.upsert.assert_called_once_with(
            [{'symbol': 'AAA'}], on_conflict='symbol')
        assert database.upsert_stock_universe_bulk([]) == 0


def test_upsert_stock_universe_bulk_error_returns_zero():
    with patch('database.supabase') as sb:
        sb.table.side_effect = Exception('down')
        assert database.upsert_stock_universe_bulk([{'symbol': 'AAA'}]) == 0


def test_update_advisor_scores_isolates_per_symbol_failure():
    with patch('database.supabase') as sb:
        calls = {'n': 0}

        def _table(name):
            calls['n'] += 1
            m = MagicMock()
            if calls['n'] == 1:
                m.update.side_effect = Exception('boom')
            return m

        sb.table.side_effect = _table
        n = database.update_advisor_scores(
            {'AAA': 50, 'BBB': -10}, '2026-07-12T10:00:00')
    assert n == 1   # BBB written despite AAA failing


def test_get_universe_by_sector_excludes_symbols():
    rows = [{'symbol': 'AAA', 'advisor_score': 80},
            {'symbol': 'BBB', 'advisor_score': 60}]
    with patch('database.supabase') as sb:
        chain = (sb.table.return_value.select.return_value
                 .eq.return_value.eq.return_value.not_.is_.return_value)
        chain.eq.return_value.order.return_value.execute.return_value.data = rows
        chain.order.return_value.execute.return_value.data = rows
        out = database.get_universe_by_sector('IT', exclude_symbols=['AAA'])
        assert [r['symbol'] for r in out] == ['BBB']
        out_all = database.get_universe_by_sector(None)
        assert len(out_all) == 2


def test_get_universe_by_sector_error_returns_empty():
    with patch('database.supabase') as sb:
        sb.table.side_effect = Exception('down')
        assert database.get_universe_by_sector('IT') == []
