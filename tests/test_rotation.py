"""Rotation advisor (Phase 2) — universe scan pacing/isolation + rotation
gate logic. ADVISORY ONLY: pins that no order path is ever touched."""
import os
from unittest.mock import MagicMock, patch

with patch.dict(os.environ, {
    'SUPABASE_URL': 'https://fake.supabase.co',
    'SUPABASE_SERVICE_KEY': 'fake-key',
}):
    with patch('supabase.create_client', return_value=MagicMock()):
        import database  # noqa

import config
import portfolio_advisor as pa


def _candles(n, start=100.0, step=0.5):
    out, p = [], start
    for i in range(n):
        p += step
        out.append({'open': p - 0.2, 'high': p + 1.0, 'low': p - 1.0,
                    'close': p, 'volume': 1000,
                    'timestamp': f'2026-01-{(i % 28) + 1:02d}'})
    return out


_UNIVERSE = [
    {'symbol': 'STRONG', 'instrument_token': 1, 'sector': 'IT'},
    {'symbol': 'WEAK', 'instrument_token': 2, 'sector': 'IT'},
    {'symbol': 'HELD', 'instrument_token': 3, 'sector': 'Banks'},
]


# --- score_universe -----------------------------------------------------------

def test_score_universe_scores_paces_and_persists():
    md = MagicMock()
    md.get_candles.return_value = _candles(300, step=0.5)   # clean uptrend
    with patch.object(pa, '_sleep') as slp, \
         patch.object(pa.db, 'upsert_stock_universe_bulk', return_value=2) as up:
        out = pa.score_universe(md, universe=_UNIVERSE,
                                exclude_symbols=['HELD'])
    assert set(out) == {'STRONG', 'WEAK'}
    assert out['STRONG']['score'] > 0
    assert out['STRONG']['sector'] == 'IT'
    assert slp.call_count == 2                     # paced per fetch
    rows = up.call_args.args[0]
    assert {r['symbol'] for r in rows} == {'STRONG', 'WEAK'}
    assert all('advisor_score' in r and 'advisor_score_updated_at' in r
               for r in rows)
    # never writes paper-engine-owned columns
    assert not any('brain_score' in r for r in rows)


def test_score_universe_isolates_per_symbol_failure_and_thin_history():
    md = MagicMock()
    md.get_candles.side_effect = [Exception('boom'), _candles(10),
                                  _candles(300)]
    with patch.object(pa, '_sleep'), \
         patch.object(pa.db, 'upsert_stock_universe_bulk', return_value=1):
        out = pa.score_universe(md, universe=_UNIVERSE)
    assert set(out) == {'HELD'}    # first errored, second too thin


def test_score_universe_never_touches_order_path():
    md = MagicMock()
    md.get_candles.return_value = _candles(300)
    with patch.object(pa, '_sleep'), \
         patch.object(pa.db, 'upsert_stock_universe_bulk', return_value=3):
        pa.score_universe(md, universe=_UNIVERSE)
    for name in ('place_buy_order', 'place_sell_order', 'place_order'):
        assert not getattr(md.kite, name).called


# --- find_rotation_candidate ---------------------------------------------------

_SCORED = {
    'ITSTRONG': {'symbol': 'ITSTRONG', 'score': 70, 'sector': 'IT'},
    'ITMED': {'symbol': 'ITMED', 'score': 55, 'sector': 'IT'},
    'AUTOTOP': {'symbol': 'AUTOTOP', 'score': 90, 'sector': 'Autos'},
}


def test_rotation_prefers_same_sector_even_over_higher_cross_sector():
    t = pa.find_rotation_candidate(-40, 'IT', _SCORED)
    assert t['symbol'] == 'ITSTRONG' and t['reason'] == 'same_sector'


def test_rotation_cross_sector_fallback():
    t = pa.find_rotation_candidate(-40, 'Pharma', _SCORED)
    assert t['symbol'] == 'AUTOTOP' and t['reason'] == 'cross_sector'


def test_rotation_gate_exit_not_weak_enough():
    assert pa.find_rotation_candidate(-10, 'IT', _SCORED) is None
    assert pa.find_rotation_candidate(None, 'IT', _SCORED) is None


def test_rotation_gate_target_and_gap():
    # target below min_target_score never surfaces
    scored = {'A': {'symbol': 'A', 'score': 45, 'sector': 'IT'}}
    assert pa.find_rotation_candidate(-40, 'IT', scored) is None
    # gap too narrow: exit -20 vs target 55 = 75 gap OK; exit 20? gated.
    scored = {'A': {'symbol': 'A', 'score': 55, 'sector': 'IT'}}
    assert pa.find_rotation_candidate(-20, 'IT', scored) is not None
    # widen min_gap beyond reach -> gated
    assert pa.find_rotation_candidate(-20, 'IT', scored, min_gap=80) is None


def test_rotation_empty_universe():
    assert pa.find_rotation_candidate(-50, 'IT', {}) is None


# --- run_advisor integration ----------------------------------------------------

def _md_with_holdings():
    md = MagicMock()
    md.kite.get_holdings.return_value = [{
        'tradingsymbol': 'LOSER', 'exchange': 'NSE', 'instrument_token': 9,
        'quantity': 10, 'average_price': 200.0, 'last_price': 100.0,
    }]
    md.kite.get_account_trades.return_value = []
    md._instrument_cache = {}
    md.get_candles.return_value = _candles(300, start=200, step=-0.4)
    return md


def test_run_advisor_attaches_rotation_when_enabled():
    md = _md_with_holdings()
    scored = {'WINNER': {'symbol': 'WINNER', 'score': 80, 'sector': 'IT'}}
    stored = {}

    def capture(rows):
        stored['rows'] = rows
        return len(rows)

    with patch.object(config, 'ROTATION_ADVISOR_ENABLED', True), \
         patch.object(pa, 'score_universe', return_value=scored) as scan, \
         patch.object(pa, 'news_sentiment', return_value=None), \
         patch.object(pa.db, 'get_tradebook', return_value=[]), \
         patch.object(pa.db, 'upsert_tradebook', return_value=0), \
         patch.object(pa.db, 'write_official_portfolio_advice', side_effect=capture):
        n = pa.run_advisor(md)

    assert n == 1
    row = stored['rows'][0]
    assert row['trend_score'] <= config.ROTATION_MAX_EXIT_SCORE
    assert row['rotation_target_symbol'] == 'WINNER'
    assert row['rotation_target_score'] == 80
    assert row['rotation_reason'] == 'cross_sector'
    assert any('Rotation: WINNER' in r for r in row['reasons'])
    # held symbols excluded from the scan
    assert scan.call_args.kwargs['exclude_symbols'] == {'LOSER'}
    for name in ('place_buy_order', 'place_sell_order', 'place_order'):
        assert not getattr(md.kite, name).called


def test_run_advisor_rotation_dark_by_default():
    md = _md_with_holdings()
    with patch.object(pa, 'score_universe') as scan, \
         patch.object(pa, 'news_sentiment', return_value=None), \
         patch.object(pa.db, 'get_tradebook', return_value=[]), \
         patch.object(pa.db, 'upsert_tradebook', return_value=0), \
         patch.object(pa.db, 'write_official_portfolio_advice', return_value=1):
        pa.run_advisor(md)
    scan.assert_not_called()


def test_run_advisor_rotation_failure_never_blocks_verdicts():
    md = _md_with_holdings()
    with patch.object(config, 'ROTATION_ADVISOR_ENABLED', True), \
         patch.object(pa, 'score_universe', side_effect=Exception('scan died')), \
         patch.object(pa, 'news_sentiment', return_value=None), \
         patch.object(pa.db, 'get_tradebook', return_value=[]), \
         patch.object(pa.db, 'upsert_tradebook', return_value=0), \
         patch.object(pa.db, 'write_official_portfolio_advice', return_value=1) as up:
        n = pa.run_advisor(md)
    assert n == 1
    assert 'rotation_target_symbol' not in up.call_args.args[0][0]
