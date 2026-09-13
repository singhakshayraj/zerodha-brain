"""On-demand single-stock lookup (score_one_symbol + scheduler serve). Mocked."""
import os, sys
from unittest.mock import MagicMock, patch
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import portfolio_advisor as pa


def _candles(n=300, start=100.0, step=0.5):
    from datetime import datetime, timedelta
    d0 = datetime(2026, 1, 1) - timedelta(days=n + 1)
    return [{'open': start + step * i, 'high': (start + step * i) * 1.01,
             'low': (start + step * i) * 0.99, 'close': start + step * i,
             'volume': 100000,
             'timestamp': (d0 + timedelta(days=i)).strftime('%Y-%m-%dT00:00:00+0530')}
            for i in range(n)]


def test_score_one_symbol_scores_and_persists():
    md = MagicMock()
    md._instrument_cache = {}
    md.get_candles.return_value = _candles()
    written = []
    with patch.object(pa.db, 'upsert_stock_universe_bulk',
                      side_effect=lambda rows: written.extend(rows)):
        row = pa.score_one_symbol(md, 'TESTCO', 12345, nifty_closes=[100.0] * 300)
    assert row['symbol'] == 'TESTCO'
    assert isinstance(row['advisor_score'], int)
    assert 'advisor_detail' in row and row['advisor_score_updated_at']
    assert written and written[0]['symbol'] == 'TESTCO'      # persisted
    assert md._instrument_cache['NSE:TESTCO'] == 12345       # token wired


def test_score_one_symbol_insufficient_bars_returns_empty():
    md = MagicMock()
    md._instrument_cache = {}
    md.get_candles.return_value = _candles(n=10)             # < MIN_DAILY_BARS
    with patch.object(pa.db, 'upsert_stock_universe_bulk') as up:
        assert pa.score_one_symbol(md, 'X', 1) == {}
    up.assert_not_called()                                    # nothing written


def test_serve_lookup_claims_request_before_working():
    """The request must be CLEARED synchronously so a second idle pass never
    double-processes it."""
    import scheduler
    cfg = {'advisor_lookup_request': '{"symbol": "INFY"}'}
    with patch.object(scheduler.db, 'get_config',
                      side_effect=lambda k: cfg.get(k)), \
         patch.object(scheduler.db, 'write_config',
                      side_effect=lambda k, v: cfg.__setitem__(k, v)), \
         patch.object(scheduler.threading, 'Thread') as T:
        scheduler._maybe_serve_lookup()
    assert cfg['advisor_lookup_request'] == ''               # claimed
    assert T.called                                          # worker spawned


def test_serve_lookup_noop_when_no_request():
    import scheduler
    with patch.object(scheduler.db, 'get_config', return_value=''), \
         patch.object(scheduler.threading, 'Thread') as T:
        scheduler._maybe_serve_lookup()
    T.assert_not_called()
