import os
from dotenv import load_dotenv

load_dotenv()

# Supabase
SUPABASE_URL = os.getenv('SUPABASE_URL')
SUPABASE_SERVICE_KEY = os.getenv('SUPABASE_SERVICE_KEY')

# Zerodha
KITE_BASE_URL = 'https://kite.zerodha.com/oms'

# Kite auto-login (token_refresher.py). All three must be set to enable;
# otherwise the manual token-paste flow is the only source of enc_token.
KITE_USER_ID = os.getenv('KITE_USER_ID')
KITE_PASSWORD = os.getenv('KITE_PASSWORD')
KITE_TOTP_SECRET = os.getenv('KITE_TOTP_SECRET')
# Old tokens expire ~6:00 AM IST; refresh daily at 6:30 so a live token is
# always in place well before the 9:15 open.
TOKEN_REFRESH_HOUR_IST = 6
TOKEN_REFRESH_MINUTE_IST = 30

# Autopilot: self-start a session each trading day at 09:30 IST without a
# dashboard START. Fires at most once per day — any session already created
# today (manual stop, loss limit, token expiry) suppresses it.
AUTOPILOT = os.getenv('AUTOPILOT', 'false').strip().lower() == 'true'

# QA mode: synthetic market (qa_market.FakeKiteClient) + market-hours bypass,
# so the full production code path can be rehearsed off-hours against the sim
# Supabase project. Never enable on the production Railway service.
QA_MODE = os.getenv('QA_MODE', 'false').strip().lower() == 'true'

# Market timing (IST)
MARKET_OPEN_HOUR = 9
MARKET_OPEN_MINUTE = 15
MARKET_CLOSE_HOUR = 15
MARKET_CLOSE_MINUTE = 20

# Brain settings
HEARTBEAT_INTERVAL_SECONDS = 60
MARKET_CONTEXT_INTERVAL_SECONDS = 900  # 15 minutes
BRAIN_VERSION = '1.0.0'


def _resolve_git_sha() -> str:
    """Deployed SHA for decision-row traceability (ENGINEERING_SPEC REQ-020,
    REQ-072). Railway injects RAILWAY_GIT_COMMIT_SHA on repo deploys; local
    runs fall back to git; tarball deploys report 'unknown'."""
    sha = os.getenv('RAILWAY_GIT_COMMIT_SHA') or os.getenv('GIT_SHA')
    if sha:
        return sha[:12]
    try:
        import subprocess
        return subprocess.run(
            ['git', 'rev-parse', '--short=12', 'HEAD'],
            capture_output=True, text=True, timeout=5,
            cwd=os.path.dirname(os.path.abspath(__file__)),
        ).stdout.strip() or 'unknown'
    except Exception:
        return 'unknown'


GIT_SHA = _resolve_git_sha()

# Risk units (ENGINEERING_SPEC §3). R = risk_per_trade_pct of capital.
# Operational daily stop is DAILY_STOP_R * R — it must fire BEFORE the
# session floor (maxLossPercent) on any normal day; the floor firing first
# is an incident (REQ-003).
RISK_PER_TRADE_PCT = float(os.getenv('RISK_PER_TRADE_PCT', '1.0'))
DAILY_STOP_R = float(os.getenv('DAILY_STOP_R', '3'))

# Trend-day tells (ENGINEERING_SPEC REQ-052, §3). Computed + logged on every
# decision during the paper run (non-gating); entry-gating flips on later.
TREND_TELLS_REQUIRED = int(os.getenv('TREND_TELLS_REQUIRED', '3'))   # of 4
VWAP_PERSISTENCE_FRAC = float(os.getenv('VWAP_PERSISTENCE_FRAC', '0.7'))
RANGE_EXPANSION_THRESHOLD = float(os.getenv('RANGE_EXPANSION_THRESHOLD', '0.6'))

# Data-quality gate (REQ-050 step 0). Stale-quote ceiling; default 2× the
# 300s poll interval.
STALE_QUOTE_MAX_S = int(os.getenv('STALE_QUOTE_MAX_S', '600'))

# In-play engine (REQ §2.1/M3). Top-N by opening-range RVOL, locked 09:30.
INPLAY_CAP = int(os.getenv('INPLAY_CAP', '10'))
RVOL_THRESHOLD = float(os.getenv('RVOL_THRESHOLD', '2.0'))

# Time-stop exit (REQ-051, §4.2/4.3c): a position that has neither hit its
# stop nor target within this many minutes is dead money — cut it. Shorts
# get a tighter clock. Flag-gated: computed + logged always, but only
# ACTS on exits when enabled, so it can be validated before it changes
# in-flight trade outcomes.
TIME_STOP_MIN = int(os.getenv('TIME_STOP_MIN', '40'))
TIME_STOP_MIN_SHORT = int(os.getenv('TIME_STOP_MIN_SHORT', '25'))
TIME_STOP_ENABLED = os.getenv('TIME_STOP_ENABLED', 'false').strip().lower() == 'true'

# Event-day policy (REQ-053): weekly expiry = Tuesday, monthly = last
# Tuesday. On event days, stand aside on index heavyweights / results-day
# symbols. Flag-gated the same way — the policy is logged on every decision,
# but only blocks entries when enabled.
EVENT_DAY_ENABLED = os.getenv('EVENT_DAY_ENABLED', 'false').strip().lower() == 'true'
# Index heavyweights to stand aside on during expiry (pin/whipsaw risk).
INDEX_HEAVYWEIGHTS = {
    'HDFCBANK', 'ICICIBANK', 'RELIANCE', 'INFY', 'TCS',
    'ITC', 'LT', 'AXISBANK', 'KOTAKBANK', 'SBIN', 'BHARTIARTL',
}

# Level filter + level-anchored stops (REQ §5 steps 6–7). Flag-gated the same
# way as time-stop/event-day: computed + logged as a counterfactual on every
# decision, but only affect entries/stops when enabled — so the level pack's
# effect can be measured before it changes trade construction.
LEVEL_FILTER_ENABLED = os.getenv('LEVEL_FILTER_ENABLED', 'false').strip().lower() == 'true'
LEVEL_STOPS_ENABLED = os.getenv('LEVEL_STOPS_ENABLED', 'false').strip().lower() == 'true'
LEVEL_PROXIMITY_BLOCK_R = float(os.getenv('LEVEL_PROXIMITY_BLOCK_R', '0.5'))
LEVEL_STOP_BUFFER_FRAC = float(os.getenv('LEVEL_STOP_BUFFER_FRAC', '0.25'))  # × ATR

# ORB archetype (REQ §5 step 5A). Second entry archetype — opening-range
# breakout. Flag-gated like the rest: the ORB signal is computed + logged on
# every decision as a counterfactual; only promotes a HOLD to an entry when
# enabled, so its edge can be measured before it changes the trade mix.
ORB_ENABLED = os.getenv('ORB_ENABLED', 'false').strip().lower() == 'true'
ORB_BREAK_BUFFER_FRAC = float(os.getenv('ORB_BREAK_BUFFER_FRAC', '0.05'))  # × OR range
# >= MIN_BUY_CONFIDENCE so a promoted ORB long clears the downstream BUY gate.
ORB_MIN_CONFIDENCE = int(os.getenv('ORB_MIN_CONFIDENCE', '70'))

# Market-direction gate. The real universe-breadth direction is ALWAYS
# computed and logged on every decision (fixing the dead SIDEWAYS stub in
# the dataset). This flag controls only whether it FEEDS the signal engine's
# buy/sell permission — default off so today's data stays comparable to the
# SIDEWAYS-fed baseline until we validate the effect, then flip with the
# other strategy flags.
MARKET_DIRECTION_ENABLED = os.getenv('MARKET_DIRECTION_ENABLED', 'false').strip().lower() == 'true'
# Breadth sanity: a per-stock day-move beyond this % is treated as a bad PDC,
# stale/wrong token, or an unadjusted corporate action — excluded from the
# breadth average instead of poisoning it (2026-07-09 logged change=123.9%
# off two garbage level-pack PDCs). And direction/breadth need a minimum
# number of clean samples before they mean anything — below it the context is
# reported low-confidence SIDEWAYS rather than a confident call off ~2 stocks.
MARKET_MAX_STOCK_MOVE_PCT = float(os.getenv('MARKET_MAX_STOCK_MOVE_PCT', '20'))
MARKET_BREADTH_MIN_SAMPLES = int(os.getenv('MARKET_BREADTH_MIN_SAMPLES', '5'))

# Paper trading: real market data + real decisions, simulated fills.
# No Kite orders are ever placed when true. See paper_broker.py.
PAPER_TRADING = os.getenv('PAPER_TRADING', 'false').strip().lower() == 'true'
PAPER_SLIPPAGE_PCT = float(os.getenv('PAPER_SLIPPAGE_PCT', '0.05'))  # adverse fill %

# ── Startup interlocks ──────────────────────────────────────────────────────
# Both failure modes here are one env-var typo away and catastrophic:
#   1. QA_MODE against the production DB writes synthetic-market garbage into
#      the real dataset with every market-hours gate bypassed.
#   2. PAPER_TRADING unset/false silently swaps PaperBroker for OrderManager
#      and places REAL Kite orders with REAL money.
# Refuse to boot instead. Called from main.py (not at import time, so unit
# tests can import config without a full env).
_PROD_DB_REF = 'gilmuwmtdpjccibfhqtx'  # zerodha-trader (production project)


def assert_safe_boot() -> None:
    if QA_MODE and SUPABASE_URL and _PROD_DB_REF in SUPABASE_URL:
        raise RuntimeError(
            'QA_MODE=true with the PRODUCTION Supabase project. '
            'QA runs only against the sim project — fix SUPABASE_URL or unset QA_MODE.'
        )

    if not PAPER_TRADING and os.getenv('REAL_TRADING_CONFIRM') != 'I-UNDERSTAND-REAL-MONEY':
        raise RuntimeError(
            'PAPER_TRADING is not true. Real order placement requires the extra '
            "env var REAL_TRADING_CONFIRM='I-UNDERSTAND-REAL-MONEY' as a second "
            'interlock. Set PAPER_TRADING=true for the paper run.'
        )

    if DATA_COLLECTION_MODE and not PAPER_TRADING:
        raise RuntimeError(
            'DATA_COLLECTION_MODE=true requires PAPER_TRADING=true. It turns the '
            'session risk stops into logged counterfactuals — never allowed with '
            'real money. Unset DATA_COLLECTION_MODE or run in paper mode.'
        )

# Risk settings
MAX_RISK_PER_TRADE_PERCENT = 1.0  # 1% of capital per trade
MAX_POSITION_SIZE_PERCENT = 20.0  # max 20% capital in one stock
MIN_TRADE_VALUE = 1000  # minimum ₹1000 per trade

# Rate limiting
QUOTE_REQUEST_DELAY_MS = 350  # 350ms between quote calls
MAX_SYMBOLS_PER_QUOTE = 500
ORDER_CONFIRMATION_WAIT_SECONDS = 2
MAX_RETRIES = 3

# Signal thresholds
MIN_BUY_CONFIDENCE = 70
MIN_SELL_CONFIDENCE = 60
MIN_RISK_REWARD_RATIO = 2.0

# Trading windows (IST)
MARKET_START_TRADING_HOUR = 9
MARKET_START_TRADING_MINUTE = 30
MARKET_NO_NEW_ENTRIES_HOUR = 15
MARKET_NO_NEW_ENTRIES_MINUTE = 0

LUNCH_START_HOUR = 12
LUNCH_START_MINUTE = 30
LUNCH_END_HOUR = 13
LUNCH_END_MINUTE = 30

# Regime
ADX_TRENDING_THRESHOLD = 25.0
ADX_WEAK_THRESHOLD = 20.0

# Circuit breaker
CIRCUIT_BREAKER_CONSECUTIVE_LOSSES = 3

# Re-entry cooldown: block re-entering a symbol within this many minutes of a
# losing exit on it (2026-07-09 re-shorted KOTAKBANK ~6min after it stopped
# out → a second −1.2R loss). Flag-gated dark feature: the would-block is
# always logged as a counterfactual; it only actually blocks when enabled,
# so we can measure the effect on collected data before turning it on.
REENTRY_COOLDOWN_ENABLED = os.getenv('REENTRY_COOLDOWN_ENABLED', 'false').strip().lower() == 'true'
REENTRY_COOLDOWN_MIN = int(os.getenv('REENTRY_COOLDOWN_MIN', '15'))

# Data-collection mode: let the session run its full day instead of throttling
# the dataset at a fixed trade count / soft loss stop. When on, the SOFT session
# limits (MAX_TRADES, DAILY_STOP_3R, session loss floor, consecutive-loss circuit
# breaker, MAX_PROFIT) become logged-not-enforced counterfactuals — trading
# continues and a LIMIT_WOULD_STOP marker records where a capped run would have
# ended. MARKET_CLOSED stays HARD-enforced. Paper-only: force off unless
# PAPER_TRADING (never relax risk with real money) — see assert_safe_boot.
DATA_COLLECTION_MODE = os.getenv('DATA_COLLECTION_MODE', 'false').strip().lower() == 'true'

# News collector (NEWS_CORRELATION_PLAN): a decoupled periodic job fetches
# ticker-tagged financial news + sentiment into news_events, out of the trading
# loop. Default OFF and dormant until a Marketaux key is provided — the collector
# no-ops without both NEWS_ENABLED and MARKETAUX_API_KEY, so nothing runs (and no
# rate-limit burn) until deliberately switched on.
NEWS_ENABLED = os.getenv('NEWS_ENABLED', 'false').strip().lower() == 'true'
MARKETAUX_API_KEY = os.getenv('MARKETAUX_API_KEY', '').strip()
NEWS_FETCH_INTERVAL_MIN = int(os.getenv('NEWS_FETCH_INTERVAL_MIN', '15'))


def data_collection_active() -> bool:
    """DATA_COLLECTION_MODE only takes effect in paper mode. Hard safety: a
    real-money session can never have its risk stops turned into counterfactuals."""
    return DATA_COLLECTION_MODE and PAPER_TRADING

# Per-cycle trade cap (spread trades across cycles, avoid burst)
MAX_TRADES_PER_CYCLE = 3

# Position sizing economics
MAX_POSITION_PERCENT = 0.40   # max 40% of capital per trade
MIN_POSITION_VALUE = 2000     # Rs2000 minimum (brokerage < 1%)
KELLY_SAFETY_MULTIPLIER = 0.33  # fractional Kelly: use 33% of full Kelly

# Trading mode — force holdings-only since /quote endpoint
# does not work on OMS for retail authentication
TRADING_MODE_FORCE = 'HOLDINGS_ONLY'

# NSE 2026 holidays (equity segment; cross-checked cleartax + groww
# against the NSE circular, 2026-07-06). Nov 8 Diwali Muhurat is a Sunday —
# weekend check covers it.
NSE_HOLIDAYS_2026 = [
    '2026-01-15',  # Maharashtra municipal elections (one-off)
    '2026-01-26',  # Republic Day
    '2026-03-03',  # Holi
    '2026-03-26',  # Shri Ram Navami
    '2026-03-31',  # Shri Mahavir Jayanti
    '2026-04-03',  # Good Friday
    '2026-04-14',  # Dr Ambedkar Jayanti
    '2026-05-01',  # Maharashtra Day
    '2026-05-28',  # Bakri Id
    '2026-06-26',  # Muharram
    '2026-09-14',  # Ganesh Chaturthi
    '2026-10-02',  # Gandhi Jayanti
    '2026-10-20',  # Dussehra
    '2026-11-10',  # Diwali Balipratipada
    '2026-11-24',  # Prakash Gurpurb
    '2026-12-25',  # Christmas
]

# NSE 2025 holidays (hardcoded)
NSE_HOLIDAYS_2025 = [
    '2025-01-26',  # Republic Day
    '2025-02-26',  # Mahashivratri
    '2025-03-14',  # Holi
    '2025-04-14',  # Dr Ambedkar Jayanti
    '2025-04-18',  # Good Friday
    '2025-05-01',  # Maharashtra Day
    '2025-08-15',  # Independence Day
    '2025-08-27',  # Ganesh Chaturthi
    '2025-10-02',  # Gandhi Jayanti
    '2025-10-21',  # Diwali Laxmi Pujan
    '2025-10-22',  # Diwali Balipratipada
    '2025-11-05',  # Prakash Gurpurb
    '2025-12-25',  # Christmas
]

# Combined lookup used by risk_manager.is_market_open()
NSE_HOLIDAYS = set(NSE_HOLIDAYS_2025) | set(NSE_HOLIDAYS_2026)

NIFTY50_INSTRUMENT_TOKENS = {
    'NSE:RELIANCE':   738561,
    'NSE:TCS':        2953217,
    'NSE:HDFCBANK':   341249,
    'NSE:INFY':       408065,
    'NSE:ICICIBANK':  1270529,
    'NSE:HINDUNILVR': 356865,
    'NSE:SBIN':       779521,
    'NSE:BHARTIARTL': 2714625,
    'NSE:KOTAKBANK':  492033,
    'NSE:LT':         2939649,
    'NSE:AXISBANK':   1510401,
    'NSE:BAJFINANCE': 81153,
    'NSE:WIPRO':      969473,
    'NSE:HCLTECH':    1850625,
    'NSE:MARUTI':     2815745,
    'NSE:SUNPHARMA':  857857,
    'NSE:TITAN':      897537,
    'NSE:POWERGRID':  3834113,
    'NSE:TMPV':       884737,  # was NSE:TATAMOTORS — renamed post 2025 CV/PV demerger
    'NSE:TATASTEEL':  895745,
    'NSE:JSWSTEEL':   3001089,
    'NSE:HINDALCO':   348929,
    'NSE:ONGC':       633601,
    'NSE:COALINDIA':  5215745,
    'NSE:BAJAJFINSV': 4268801,
    'NSE:DRREDDY':    225537,
    'NSE:CIPLA':      177665,
}

# NIFTY Next 50 constituents (as of 2026-07), tokens verified against
# Kite's public instrument master (api.kite.trade/instruments/NSE) —
# get_instruments() is stubbed dead on this retail OMS (kite_client.py),
# so tokens can't be resolved dynamically and must be pinned like NIFTY50.
NIFTY_NEXT50_INSTRUMENT_TOKENS = {
    'NSE:ADANIPOWER':  4451329,
    'NSE:HAL':         589569,
    'NSE:DMART':       5097729,
    'NSE:ADANIGREEN':  912129,
    'NSE:HINDZINC':    364545,
    'NSE:ADANIENSOL':  2615553,
    'NSE:IOC':         415745,
    'NSE:DIVISLAB':    2800641,
    'NSE:TVSMOTOR':    2170625,
    'NSE:TORNTPHARM':  900609,
    'NSE:SOLARINDS':   3412993,
    'NSE:VBL':         4843777,
    'NSE:PIDILITIND':  681985,
    'NSE:DLF':         3771393,
    'NSE:HYUNDAI':     6616065,
    'NSE:TMCV':        194504193,
    'NSE:CUMMINSIND':  486657,
    'NSE:CHOLAFIN':    175361,
    'NSE:MOTHERSON':   1076225,
    'NSE:TATACAP':     194371841,
    'NSE:ABB':         3329,
    'NSE:CGPOWER':     194561,
    'NSE:PFC':         3660545,
    'NSE:BPCL':        134657,
    'NSE:BRITANNIA':   140033,
    'NSE:BANKBARODA':  1195009,
    'NSE:MUTHOOTFIN':  6054401,
    'NSE:BOSCHLTD':    558337,
    'NSE:SIEMENS':     806401,
    'NSE:ENRIN':       193758977,
    'NSE:TATAPOWER':   877057,
    'NSE:UNIONBANK':   2752769,
    'NSE:PNB':         2730497,
    'NSE:ZYDUSLIFE':   2029825,
    'NSE:BAJAJHLDNG':  78081,
    'NSE:IRFC':        519425,
    'NSE:HDFCAMC':     1086465,
    'NSE:LTM':         4561409,
    'NSE:LODHA':       824321,
    'NSE:GAIL':        1207553,
    'NSE:GODREJCP':    2585345,
    'NSE:CANBK':       2763265,
    'NSE:VEDL':        784129,
    'NSE:AMBUJACEM':   325121,
    'NSE:JINDALSTEL':  1723649,
    'NSE:INDHOTEL':    387073,
    'NSE:UNITDSPR':    2674433,
    'NSE:MAZDOCK':     130305,
    'NSE:SHREECEM':    794369,
    'NSE:RECLTD':      3930881,
}


# ── Nifty 500 universe (Portfolio Advisor rotation scan) ────────────────────
# Same "pin it, don't fetch it live" philosophy as the dicts above, but at 500
# symbols the pin lives in data/nifty500.csv (symbol, instrument_token,
# sector, industry, company_name), regenerated per quarterly index
# reconstitution via scripts/build_nifty500_tokens.py. Loaded once at import;
# a missing/corrupt file degrades to an empty list — the rotation scan then
# simply has no candidates, never a crash.
def _load_nifty500_universe() -> list:
    import csv as _csv
    path = os.path.join(os.path.dirname(__file__), 'data', 'nifty500.csv')
    try:
        with open(path, newline='') as f:
            rows = []
            for r in _csv.DictReader(f):
                try:
                    rows.append({
                        'symbol': r['symbol'],
                        'instrument_token': int(r['instrument_token']),
                        'sector': r.get('sector') or None,
                        'industry': r.get('industry') or None,
                        'company_name': r.get('company_name') or None,
                    })
                except Exception:
                    continue
            return rows
    except Exception as e:
        print(f"[config] nifty500.csv unavailable ({e}) — universe scan empty")
        return []


NIFTY500_UNIVERSE = _load_nifty500_universe()
NIFTY500_INSTRUMENT_TOKENS = {
    f"NSE:{r['symbol']}": r['instrument_token'] for r in NIFTY500_UNIVERSE
}

# ── Rotation advisor (Portfolio Advisor phase 2) ────────────────────────────
# Dark by default: the daily Nifty 500 scan + rotation suggestions only run
# when this is flipped on Railway. Advisory only — nothing here can place an
# order.
ROTATION_ADVISOR_ENABLED = os.getenv(
    'ROTATION_ADVISOR_ENABLED', 'false').strip().lower() == 'true'
# Pace between universe candle fetches; 500 symbols × 350ms ≈ 3 min once/day,
# same cadence the quote path already treats as safe for this Kite session.
ADVISOR_UNIVERSE_SCAN_DELAY_MS = int(
    os.getenv('ADVISOR_UNIVERSE_SCAN_DELAY_MS', '350'))
# Rotation gate: only surface a swap when the exit is genuinely weak AND the
# target is genuinely strong AND the spread between them is wide — "rotate
# into strength", never "rotate into least-bad".
ROTATION_MAX_EXIT_SCORE = int(os.getenv('ROTATION_MAX_EXIT_SCORE', '-20'))
ROTATION_MIN_TARGET_SCORE = int(os.getenv('ROTATION_MIN_TARGET_SCORE', '50'))
ROTATION_MIN_GAP = int(os.getenv('ROTATION_MIN_GAP', '40'))

# ── Live-tunable signal knobs (REQ-030) ─────────────────────────────────────
# A whitelisted set of SIGNAL thresholds can be overridden at runtime from the
# app_config 'tunables' key (a JSON object) WITHOUT a redeploy — useful under
# the market-hours deploy freeze. Risk-sizing/stop knobs are deliberately NOT
# here: those stay code-only so nothing mutable at runtime can relax the money
# path. Reads are cached (TTL) and fail safe — any DB/parse error falls back to
# the compiled default, never crashing the trading loop.
import time as _time
import json as _json

TUNABLE_DEFAULTS = {
    'MIN_BUY_CONFIDENCE': MIN_BUY_CONFIDENCE,
    'MIN_SELL_CONFIDENCE': MIN_SELL_CONFIDENCE,
    'MIN_RISK_REWARD_RATIO': MIN_RISK_REWARD_RATIO,
    'ADX_TRENDING_THRESHOLD': ADX_TRENDING_THRESHOLD,
    'ADX_WEAK_THRESHOLD': ADX_WEAK_THRESHOLD,
}
TUNABLE_TTL_SECONDS = 60
_tunable_cache: dict = {}
_tunable_cache_ts: float = 0.0


def _tunable_overrides() -> dict:
    """Cached JSON map of overrides from app_config 'tunables'. Refreshed every
    TUNABLE_TTL_SECONDS. On any failure keeps the last-known cache (or empty)
    so the trading loop never breaks on a transient DB error. Lazy DB import
    avoids the config↔database import cycle."""
    global _tunable_cache, _tunable_cache_ts
    now = _time.monotonic()
    if _tunable_cache_ts and (now - _tunable_cache_ts) < TUNABLE_TTL_SECONDS:
        return _tunable_cache
    try:
        import database
        raw = database.get_config('tunables')
        parsed = _json.loads(raw) if raw else {}
        _tunable_cache = parsed if isinstance(parsed, dict) else {}
    except Exception as e:
        print(f"[config.tunables] override read failed (non-fatal): {e}")
    _tunable_cache_ts = now
    return _tunable_cache


def get_tunable(key: str):
    """Return the live override for a whitelisted signal knob, else its compiled
    default. Coerced to the default's type; bad values fall back to default."""
    default = TUNABLE_DEFAULTS[key]  # KeyError = using a non-whitelisted key
    override = _tunable_overrides().get(key)
    if override is None:
        return default
    try:
        return type(default)(override)
    except (ValueError, TypeError):
        print(f"[config.tunables] bad override {key}={override!r}; using default")
        return default
