"""
Non-sensitive engine settings.
"""

# =========================================================
# MARKET DATA
# =========================================================

SYMBOL = "NIFTY"

EXCHANGE = "INDEX"

TIMEFRAME = "60"


# =========================================================
# ENGINE MODES
# =========================================================

PAPER_TRADE = True

LIVE_TRADE = False


# =========================================================
# CAPITAL & RISK
# =========================================================

CAPITAL = 100000.0

RISK_PER_TRADE_PCT = 0.02

MAX_TRADES_PER_DAY = 1

MAX_MARGIN_UTILIZATION_PCT = 0.30

BUFFER_MARGIN_PCT = 0.10

ESTIMATED_MARGIN_PER_LOT = 45000.0


# =========================================================
# OPTION SPREAD RULES
# =========================================================

MAX_SPREAD_WIDTH = 300

MIN_CREDIT = 0.0

MAX_SLIPPAGE_PCT = 0.15

TRAIL_TO_BREAKEVEN_PCT = 0.35


# =========================================================
# STRATEGY PARAMETERS
# =========================================================

SUPERTREND_ATR = 10

SUPERTREND_MULTIPLIER = 3.0

BREAKOUT_LOOKBACK = 5

MAX_EXTENSION_ATR = 0.35

MAX_CANDLE_RANGE_ATR = 1.5

MIN_ATR = 50.0


# =========================================================
# EXECUTION
# =========================================================

POLL_INTERVAL_SECONDS = 60

FAST_POLL_INTERVAL_SECONDS = 5

ENABLE_TELEGRAM_ALERTS = True

ENABLE_KILL_SWITCH = True