"""News collector (NEWS_CORRELATION_PLAN) — decoupled from the trading loop.

A periodic job fetches ticker-tagged financial news + sentiment from Marketaux
and stores normalized rows in news_events. Decisions later attach a CACHED
news_context (a DB read, never a live API call in the hot loop).

Dormant by default: collect() no-ops unless BOTH config.NEWS_ENABLED is true and
config.MARKETAUX_API_KEY is set — so nothing runs, and no rate-limit is burned,
until the key is provided and the flag flipped. The normalize step is pure and
unit-tested against a sample payload, so the parsing is validated before any
live key exists.

Marketaux free tier ~100 req/day → batch symbols per call (entity filter) and
poll on an interval (config.NEWS_FETCH_INTERVAL_MIN), not per-symbol per-minute.
"""
import requests

import config
import database as db

_MARKETAUX_URL = 'https://api.marketaux.com/v1/news/all'


def _label(score):
    if score is None:
        return None
    if score > 0.15:
        return 'positive'
    if score < -0.15:
        return 'negative'
    return 'neutral'


def normalize_marketaux(payload: dict) -> list:
    """Marketaux /news/all response → news_events rows. One row per article;
    symbols = the article's tagged entity symbols; sentiment_score = mean of the
    entity sentiment scores (Marketaux scores per entity). Pure + safe: skips
    malformed articles rather than raising."""
    rows = []
    for art in (payload or {}).get('data', []) or []:
        try:
            url = art.get('url')
            if not url:
                continue
            entities = art.get('entities') or []
            # Marketaux tags tickers with an exchange suffix (RELIANCE.NSE);
            # strip it so symbols match our universe form (RELIANCE).
            symbols = sorted({e['symbol'].split('.')[0] for e in entities
                              if e.get('symbol')})
            scores = [e['sentiment_score'] for e in entities
                      if e.get('sentiment_score') is not None]
            score = round(sum(scores) / len(scores), 4) if scores else None
            rows.append({
                'source': 'marketaux',
                'published_at': art.get('published_at'),
                'scope': 'STOCK' if symbols else 'MACRO',
                'symbols': symbols,
                'headline': art.get('title'),
                'url': url,
                'sentiment_score': score,
                'sentiment_label': _label(score),
                'raw': art,
            })
        except Exception as e:
            print(f"[news_jobs.normalize] skipped an article: {e}")
    return rows


def fetch_marketaux(symbols: list, api_key: str, limit: int = 25) -> dict:
    """One Marketaux call for a batch of symbols. Raises on HTTP error — the
    caller (collect) guards and logs."""
    params = {
        'api_token': api_key,
        'symbols': ','.join(symbols) if symbols else None,
        'filter_entities': 'true',
        'language': 'en',
        'limit': limit,
    }
    params = {k: v for k, v in params.items() if v is not None}
    resp = requests.get(_MARKETAUX_URL, params=params, timeout=10)
    resp.raise_for_status()
    return resp.json()


def collect(symbols: list) -> int:
    """Fetch → normalize → store one batch. Returns the number of rows upserted.
    No-ops (returns 0) when disabled or unconfigured, so it is safe to call on a
    schedule regardless of setup."""
    if not config.NEWS_ENABLED or not config.MARKETAUX_API_KEY:
        return 0
    try:
        payload = fetch_marketaux(symbols, config.MARKETAUX_API_KEY)
    except Exception as e:
        print(f"[news_jobs.collect] fetch failed (non-fatal): {e}")
        return 0
    rows = normalize_marketaux(payload)
    return db.upsert_news_events(rows)
