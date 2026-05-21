import os
from dotenv import load_dotenv

load_dotenv()

# Supabase
SUPABASE_URL = os.getenv('SUPABASE_URL')
SUPABASE_SERVICE_KEY = os.getenv('SUPABASE_SERVICE_KEY')

# Zerodha
KITE_BASE_URL = 'https://kite.zerodha.com/oms'

# Market timing (IST)
MARKET_OPEN_HOUR = 9
MARKET_OPEN_MINUTE = 15
MARKET_CLOSE_HOUR = 15
MARKET_CLOSE_MINUTE = 20

# Brain settings
HEARTBEAT_INTERVAL_SECONDS = 60
MARKET_CONTEXT_INTERVAL_SECONDS = 900  # 15 minutes
BRAIN_VERSION = '1.0.0'

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

# Trading mode — force holdings-only since /quote endpoint
# does not work on OMS for retail authentication
TRADING_MODE_FORCE = 'HOLDINGS_ONLY'

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
