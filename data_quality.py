"""Data-quality gate — ENGINEERING_SPEC REQ-050 step 0.

Runs before any indicator/signal work: a stale, missing, or insane quote
must QUARANTINE the symbol for this cycle (a logged SKIP), never feed a
trade. Bad market data is the cheapest way to a wrong fill; the pipeline is
only as trustworthy as its inputs.

Pure and individually testable. check_quote returns (ok, reason_code):
ok=True  → analyze the symbol
ok=False → skip with reason_code (QUARANTINE_*), logged as a decision.
"""

import config

# Sanity band: a live price this far from the latest candle close almost
# always means a wrong instrument token or a corrupt quote, not a real move.
MAX_PRICE_DEVIATION = 0.20  # 20%


def check_quote(live_price, last_candle_close=None, quote_age_s=None,
                max_stale_s=None) -> tuple:
    """Validate a single symbol's quote for this cycle.

    live_price       : price the brain would act on (holdings cache / candle)
    last_candle_close: most recent candle close for cross-check (optional)
    quote_age_s      : seconds since the quote was produced, if known
    max_stale_s      : staleness ceiling (defaults to 2× poll interval)
    """
    if max_stale_s is None:
        max_stale_s = getattr(config, 'STALE_QUOTE_MAX_S', 600)

    if live_price is None or live_price <= 0:
        return False, 'QUARANTINE_NO_PRICE'

    # NaN / inf guard (never trust arithmetic on a non-finite quote)
    if live_price != live_price or live_price in (float('inf'), float('-inf')):
        return False, 'QUARANTINE_NONFINITE'

    if quote_age_s is not None and quote_age_s > max_stale_s:
        return False, 'QUARANTINE_STALE'

    if last_candle_close and last_candle_close > 0:
        deviation = abs(live_price - last_candle_close) / last_candle_close
        if deviation > MAX_PRICE_DEVIATION:
            return False, 'QUARANTINE_PRICE_DEVIATION'

    return True, None
