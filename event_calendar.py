"""Event-day policy — ENGINEERING_SPEC REQ-053.

Mechanical calendar rules that raise the bar or stand aside on known-noise
days: NSE weekly options expiry (Tuesday) and monthly expiry (last Tuesday
of the month) pin/whipsaw index heavyweights; results-day symbols are stood
aside through the event.

Pure and testable — no DB here. Results-day symbols come from an external
earnings feed the caller supplies (empty until that feed exists; the expiry
rules work today with just the date).

policy() returns one of:
  'NORMAL'      trade as usual
  'RAISE_BAR'   allowed, but require a higher confidence bar
  'STAND_ASIDE' no entries in this symbol today
"""

import calendar as _cal
from datetime import date as _date

import config

NORMAL = 'NORMAL'
RAISE_BAR = 'RAISE_BAR'
STAND_ASIDE = 'STAND_ASIDE'


def _as_date(d):
    if isinstance(d, _date):
        return d
    # accept 'YYYY-MM-DD' or ISO timestamp
    return _date.fromisoformat(str(d)[:10])


def is_weekly_expiry(d) -> bool:
    """NSE index weekly options expiry — Tuesday (weekday 1)."""
    return _as_date(d).weekday() == 1


def is_monthly_expiry(d) -> bool:
    """Monthly expiry — the LAST Tuesday of the month."""
    d = _as_date(d)
    if d.weekday() != 1:
        return False
    # last Tuesday = no other Tuesday later this month
    last_day = _cal.monthrange(d.year, d.month)[1]
    return d.day + 7 > last_day


def is_index_heavyweight(symbol: str) -> bool:
    return symbol.upper() in config.INDEX_HEAVYWEIGHTS


def policy(d, symbol: str, results_symbols=None) -> dict:
    """Effective event policy for (date, symbol).

    results_symbols: iterable of symbols reporting earnings today (stand
    aside). None/empty = no earnings feed available.
    """
    reasons = []
    result = NORMAL

    results_symbols = {s.upper() for s in (results_symbols or [])}
    if symbol.upper() in results_symbols:
        return {'policy': STAND_ASIDE, 'reasons': ['RESULTS_DAY'],
                'weekly_expiry': is_weekly_expiry(d),
                'monthly_expiry': is_monthly_expiry(d)}

    monthly = is_monthly_expiry(d)
    weekly = is_weekly_expiry(d)

    if (monthly or weekly) and is_index_heavyweight(symbol):
        # heavyweights get pinned on expiry — stand aside on monthly (the
        # worst), raise the bar on weekly
        if monthly:
            result = STAND_ASIDE
            reasons.append('MONTHLY_EXPIRY_HEAVYWEIGHT')
        else:
            result = RAISE_BAR
            reasons.append('WEEKLY_EXPIRY_HEAVYWEIGHT')

    return {'policy': result, 'reasons': reasons,
            'weekly_expiry': weekly, 'monthly_expiry': monthly}
