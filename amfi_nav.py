"""AMFI NAV ingestion — the whole-book precondition.

Fund-of-funds and other mutual funds are NAV-based, not exchange-traded: no
Kite instrument token, no candle endpoint. This pulls AMFI's daily published
NAV file (public, no auth) so those holdings can be priced at all.

The file is semicolon-delimited plain text:
    Scheme Code;ISIN Div Payout/ISIN Growth;ISIN Div Reinvestment;\
Scheme Name;Net Asset Value;Date
with AMC/category header lines interspersed (no ';'), which are skipped.

Pure parse (`parse_navall`) is unit-tested; the fetch is a thin wrapper.
Mapping held schemes -> scheme codes is HAND-CURATED elsewhere (holding_scheme_map);
this module never guesses which scheme a holding is.
"""
import datetime as _dt

import requests

# The canonical URL 302-redirects to portal.amfiindia.com; requests follows it.
# A browser User-Agent is REQUIRED: the portal's WAF serves an HTML challenge
# (not the NAV text) to a default python-requests UA, which parsed to 0 rows
# silently in prod (2026-09-10). Mirror a browser.
NAVALL_URL = 'https://www.amfiindia.com/spages/NAVAll.txt'
_UA = ('Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
       '(KHTML, like Gecko) Chrome/124.0 Safari/537.36')


def parse_navall(text: str) -> list:
    """-> [{scheme_code, scheme_name, nav, nav_date}]. Rows with a
    non-numeric NAV ('N.A.') or an unparseable date are skipped, not guessed."""
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line or ';' not in line:
            continue                      # AMC / category header, or blank
        parts = line.split(';')
        if len(parts) < 6 or parts[0].strip().lower() == 'scheme code':
            continue                      # column header or malformed
        code = parts[0].strip()
        name = parts[3].strip()
        nav_raw = parts[4].strip()
        date_raw = parts[5].strip()
        if not code.isdigit():
            continue
        try:
            nav = float(nav_raw)
        except ValueError:
            continue                      # 'N.A.' — no NAV published today
        try:
            nav_date = _dt.datetime.strptime(date_raw, '%d-%b-%Y').date().isoformat()
        except ValueError:
            continue
        out.append({'scheme_code': code, 'scheme_name': name,
                    'nav': nav, 'nav_date': nav_date})
    return out


def fetch_navall(timeout: int = 60) -> str:
    """Raw NAVAll.txt text. Raises on HTTP error, or when the body is not the
    semicolon-delimited NAV file (a WAF/HTML page) — so a blocked fetch fails
    loudly rather than parsing to 0 rows in silence."""
    r = requests.get(NAVALL_URL, timeout=timeout,
                     headers={'User-Agent': _UA, 'Accept': 'text/plain,*/*'})
    r.raise_for_status()
    text = r.text
    if 'Scheme Code' not in text and text.count(';') < 100:
        raise RuntimeError(
            f"AMFI NAV fetch did not return the NAV file "
            f"(len={len(text)}, ';'-count={text.count(';')}) — likely a WAF/HTML "
            f"response. First 120 chars: {text[:120]!r}")
    return text


def fetch_and_parse() -> list:
    return parse_navall(fetch_navall())
