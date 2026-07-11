"""Per-decision news_context (NEWS_CORRELATION_PLAN): the in-memory cache the
hot loop reads (no live API/DB call), plus the throttled fetch gating."""
import os
from datetime import timedelta, datetime
from unittest.mock import MagicMock, patch

with patch.dict(os.environ, {
    'SUPABASE_URL': 'https://fake.supabase.co',
    'SUPABASE_SERVICE_KEY': 'fake-key',
}):
    with patch('supabase.create_client', return_value=MagicMock()):
        import database  # noqa

import config
from brain import TradingBrain, IST


def _brain():
    b = TradingBrain.__new__(TradingBrain)
    b._news_cache = {}
    b._last_news_fetch = None
    b._news_fetching = False
    b.universe = {'NSE:RELIANCE': {}, 'NSE:TCS': {}}
    return b


def _row(sym, pub, score, label, headline):
    return {'symbols': [sym], 'published_at': pub, 'sentiment_score': score,
            'sentiment_label': label, 'headline': headline}


def test_rebuild_cache_keeps_latest_per_symbol():
    b = _brain()
    b._rebuild_news_cache([
        _row('RELIANCE', '2026-07-10T04:00:00Z', 0.2, 'positive', 'older'),
        _row('RELIANCE', '2026-07-10T05:00:00Z', 0.6, 'positive', 'newer'),
    ])
    assert b._news_cache['RELIANCE']['headline'] == 'newer'
    assert b._news_cache['RELIANCE']['sentiment_score'] == 0.6


def test_news_context_unavailable_without_cache():
    assert _brain()._news_context('RELIANCE') == {'available': False}


def test_news_context_available_with_minutes_since_headline():
    b = _brain()
    pub = (datetime.now(IST) - timedelta(minutes=30)).isoformat()
    b._news_cache['RELIANCE'] = {'published_at': pub, 'sentiment_score': 0.5,
                                 'sentiment_label': 'positive', 'headline': 'h'}
    ctx = b._news_context('RELIANCE')
    assert ctx['available'] is True
    assert ctx['sentiment_label'] == 'positive'
    assert 25 < ctx['minutes_since_headline'] < 35   # ~30m


def test_news_context_bad_timestamp_gives_none_minutes():
    b = _brain()
    b._news_cache['TCS'] = {'published_at': 'nope', 'sentiment_score': None,
                            'sentiment_label': None, 'headline': 'h'}
    ctx = b._news_context('TCS')
    assert ctx['available'] is True
    assert ctx['minutes_since_headline'] is None


def test_maybe_collect_noop_when_disabled():
    b = _brain()
    with patch.object(config, 'NEWS_ENABLED', False), \
         patch.object(config, 'MARKETAUX_API_KEY', 'k'), \
         patch('brain.threading.Thread') as T:
        b._maybe_collect_news()
    T.assert_not_called()
    assert b._last_news_fetch is None


def test_maybe_collect_spawns_thread_when_enabled():
    b = _brain()
    with patch.object(config, 'NEWS_ENABLED', True), \
         patch.object(config, 'MARKETAUX_API_KEY', 'k'), \
         patch('brain.threading.Thread') as T:
        b._maybe_collect_news()
    T.assert_called_once()
    assert b._last_news_fetch is not None


def test_maybe_collect_throttled_within_interval():
    b = _brain()
    b._last_news_fetch = datetime.now(IST)   # just fetched
    with patch.object(config, 'NEWS_ENABLED', True), \
         patch.object(config, 'MARKETAUX_API_KEY', 'k'), \
         patch('brain.threading.Thread') as T:
        b._maybe_collect_news()
    T.assert_not_called()
