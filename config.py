import os
from dotenv import load_dotenv

load_dotenv()

# Supabase
SUPABASE_URL = os.getenv('SUPABASE_URL')
SUPABASE_SERVICE_KEY = os.getenv('SUPABASE_SERVICE_KEY')

# Zerodha
KITE_BASE_URL = 'https://api.kite.trade'

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
MIN_BUY_CONFIDENCE = 65
MIN_SELL_CONFIDENCE = 60
MIN_RISK_REWARD_RATIO = 1.5

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
