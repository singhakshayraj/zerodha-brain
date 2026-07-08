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
    'NSE:TATAMOTORS': 884737,
    'NSE:TATASTEEL':  895745,
    'NSE:JSWSTEEL':   3001089,
    'NSE:HINDALCO':   348929,
    'NSE:ONGC':       633601,
    'NSE:COALINDIA':  5215745,
    'NSE:BAJAJFINSV': 4268801,
    'NSE:DRREDDY':    225537,
    'NSE:CIPLA':      177665,
}
