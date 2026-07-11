"""News collector (NEWS_CORRELATION_PLAN): Marketaux payload normalization +
the dormant-by-default gating (no key / disabled → no-op). The parse is tested
against a sample payload so it's validated before any live key exists."""
import os
from unittest.mock import MagicMock, patch

with patch.dict(os.environ, {
    'SUPABASE_URL': 'https://fake.supabase.co',
    'SUPABASE_SERVICE_KEY': 'fake-key',
}):
    with patch('supabase.create_client', return_value=MagicMock()):
        import database  # noqa

import config
import news_jobs


_SAMPLE = {
    'data': [
        {
            'uuid': 'a1', 'title': 'Reliance beats estimates',
            'url': 'https://news.example/1',
            'published_at': '2026-07-10T05:00:00.000000Z',
            'entities': [
                {'symbol': 'RELIANCE.NSE', 'sentiment_score': 0.6},
                {'symbol': 'TCS.NSE', 'sentiment_score': 0.2},
            ],
        },
        {
            'uuid': 'a2', 'title': 'Global macro jitters',
            'url': 'https://news.example/2',
            'published_at': '2026-07-10T04:30:00.000000Z',
            'entities': [],
        },
        {'uuid': 'a3', 'title': 'no url dropped', 'entities': []},  # skipped
    ]
}


def test_normalize_maps_articles_and_strips_exchange_suffix():
    rows = news_jobs.normalize_marketaux(_SAMPLE)
    assert len(rows) == 2                       # the url-less article dropped
    stock = rows[0]
    assert stock['symbols'] == ['RELIANCE', 'TCS']   # .NSE stripped, sorted
    assert stock['scope'] == 'STOCK'
    assert stock['source'] == 'marketaux'
    assert stock['sentiment_score'] == 0.4      # mean of 0.6, 0.2
    assert stock['sentiment_label'] == 'positive'


def test_normalize_macro_when_no_entities():
    rows = news_jobs.normalize_marketaux(_SAMPLE)
    macro = rows[1]
    assert macro['symbols'] == []
    assert macro['scope'] == 'MACRO'
    assert macro['sentiment_score'] is None
    assert macro['sentiment_label'] is None


def test_normalize_empty_payload_safe():
    assert news_jobs.normalize_marketaux({}) == []
    assert news_jobs.normalize_marketaux({'data': None}) == []


def test_label_thresholds():
    assert news_jobs._label(0.5) == 'positive'
    assert news_jobs._label(-0.5) == 'negative'
    assert news_jobs._label(0.0) == 'neutral'
    assert news_jobs._label(None) is None


# --- gating: dormant until enabled AND keyed ---

def test_collect_noop_when_disabled():
    with patch.object(config, 'NEWS_ENABLED', False), \
         patch.object(config, 'MARKETAUX_API_KEY', 'k'):
        assert news_jobs.collect(['RELIANCE']) == 0


def test_collect_noop_without_key():
    with patch.object(config, 'NEWS_ENABLED', True), \
         patch.object(config, 'MARKETAUX_API_KEY', ''):
        assert news_jobs.collect(['RELIANCE']) == 0


def test_collect_fetches_normalizes_stores_when_configured():
    with patch.object(config, 'NEWS_ENABLED', True), \
         patch.object(config, 'MARKETAUX_API_KEY', 'k'), \
         patch.object(news_jobs, 'fetch_marketaux', return_value=_SAMPLE), \
         patch.object(news_jobs.db, 'upsert_news_events', return_value=2) as up:
        n = news_jobs.collect(['RELIANCE', 'TCS'])
    assert n == 2
    # stored the normalized rows, not the raw payload
    up.assert_called_once()
    assert len(up.call_args.args[0]) == 2


def test_collect_survives_fetch_error():
    with patch.object(config, 'NEWS_ENABLED', True), \
         patch.object(config, 'MARKETAUX_API_KEY', 'k'), \
         patch.object(news_jobs, 'fetch_marketaux', side_effect=RuntimeError('429')):
        assert news_jobs.collect(['RELIANCE']) == 0
