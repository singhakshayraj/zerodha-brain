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
import os

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
            # Marketaux tags tickers with an exchange suffix (RELIANCE.NS);
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


def fetch_marketaux(symbols: list, api_key: str, limit: int = 3,
                    published_after: str = None, published_before: str = None,
                    page: int = None) -> dict:
    """One Marketaux call for a batch of symbols. published_after/before (ISO or
    YYYY-MM-DD) + page drive historical backfill. Raises on HTTP error — the
    caller guards and logs. limit defaults to 3 = the Marketaux free-plan cap
    (higher values just warn and return 3 anyway)."""
    params = {
        'api_token': api_key,
        'symbols': ','.join(symbols) if symbols else None,
        'filter_entities': 'true',
        'language': 'en',
        'limit': limit,
        'published_after': published_after,
        'published_before': published_before,
        'page': page,
    }
    params = {k: v for k, v in params.items() if v is not None}
    resp = requests.get(_MARKETAUX_URL, params=params, timeout=10)
    resp.raise_for_status()
    return resp.json()


def backfill(symbols: list, published_after: str, published_before: str,
             pages: int = 3, batch_size: int = 10, sleep_s: float = 1.0) -> int:
    """Historical fill of news_events for past session dates so already-stored
    trades can correlate via a symbol + published_at<decided_at join (no
    re-stamp of old decisions). Paced for the free tier (~100 req/day): symbols
    are chunked, pages are bounded, and a sleep spaces requests. Returns total
    rows upserted. No-op (0) unless enabled + keyed."""
    if not config.NEWS_ENABLED or not config.MARKETAUX_API_KEY:
        print("[news_jobs.backfill] disabled or unkeyed — nothing to do")
        return 0
    total = 0
    for i in range(0, len(symbols), batch_size):
        chunk = symbols[i:i + batch_size]
        for page in range(1, pages + 1):
            try:
                payload = fetch_marketaux(
                    chunk, config.MARKETAUX_API_KEY,
                    published_after=published_after,
                    published_before=published_before, page=page)
            except Exception as e:
                print(f"[news_jobs.backfill] fetch failed chunk={chunk} "
                      f"page={page} (stopping this chunk): {e}")
                break
            rows = normalize_marketaux(payload)
            if not rows:
                break                    # no more articles for this chunk
            total += db.upsert_news_events(rows)
            _sleep(sleep_s)
    print(f"[news_jobs.backfill] upserted {total} rows over "
          f"{len(symbols)} symbols {published_after}..{published_before}")
    return total


def backfill_from_trades(published_after: str, published_before: str,
                         **kw) -> int:
    """Backfill news for exactly the symbols we've traded, over a date window.
    Symbols derived from the trades table so the fill targets what we can
    correlate. Run as a one-off (see __main__)."""
    symbols = [f"{s}.NS" for s in db.traded_symbols()]
    if not symbols:
        print("[news_jobs.backfill_from_trades] no traded symbols found")
        return 0
    return backfill(symbols, published_after, published_before, **kw)


def run_backfill_from_env() -> int:
    """Boot hook: if NEWS_BACKFILL_WINDOW='YYYY-MM-DD,YYYY-MM-DD' is set, run a
    one-off historical backfill on startup (no shell needed — set the var +
    restart). Idempotent (upsert dedups on source,url), so leaving the var set
    across restarts is harmless; remove it once filled. No-op when unset/blank
    or the collector is disabled."""
    window = os.getenv('NEWS_BACKFILL_WINDOW', '').strip()
    if not window:
        return 0
    parts = [p.strip() for p in window.split(',')]
    if len(parts) != 2 or not all(parts):
        print(f"[news_jobs] NEWS_BACKFILL_WINDOW must be 'AFTER,BEFORE' "
              f"(got {window!r}) — skipping")
        return 0
    after, before = parts
    print(f"[news_jobs] boot backfill {after}..{before}")
    try:
        n = backfill_from_trades(after, before)
        print(f"[news_jobs] boot backfill done: {n} rows")
        return n
    except Exception as e:
        print(f"[news_jobs] boot backfill failed (non-fatal): {e}")
        return 0


def _sleep(seconds: float) -> None:
    """Indirection so tests can patch out the rate-limit pause."""
    import time
    time.sleep(seconds)


if __name__ == '__main__':
    # One-off historical backfill:
    #   python news_jobs.py <published_after> <published_before>
    # dates as YYYY-MM-DD. Needs NEWS_ENABLED=true + MARKETAUX_API_KEY in env.
    import sys
    if len(sys.argv) != 3:
        print("usage: python news_jobs.py <YYYY-MM-DD after> <YYYY-MM-DD before>")
        sys.exit(1)
    n = backfill_from_trades(sys.argv[1], sys.argv[2])
    print(f"backfilled {n} news rows")


def collect(symbols: list) -> list:
    """Fetch → normalize → store one batch. Returns the normalized rows (so a
    caller can also refresh an in-memory cache). No-ops (returns []) when
    disabled or unconfigured, so it is safe to call on a schedule regardless of
    setup."""
    if not config.NEWS_ENABLED or not config.MARKETAUX_API_KEY:
        return []
    try:
        payload = fetch_marketaux(symbols, config.MARKETAUX_API_KEY)
    except Exception as e:
        print(f"[news_jobs.collect] fetch failed (non-fatal): {e}")
        return []
    rows = normalize_marketaux(payload)
    db.upsert_news_events(rows)
    return rows
