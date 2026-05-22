# config/settings.py
from datetime import time

# =========================================================
# SYSTEM TOGGLES
# =========================================================
PAPER_TRADE = False

# =========================================================
# CAPITAL & RISK
# =========================================================
CAPITAL_UTILIZATION    = 0.95   # Keep 5% buffer unused

# ₹50,000 real capital × 4.5x intraday margin = ₹2,25,000 effective
CAPITAL                = 225000.0

# 1% of real capital (₹500) / effective (₹2,25,000) = 0.22%
MAX_RISK_PER_TRADE_PCT = 0.22

# 5 trades × ₹500 = ₹2,500 worst case = 1.1% of effective
MAX_DAILY_LOSS_PCT     = 1.5

# =========================================================
# CAPACITY LIMITS
# =========================================================
MAX_TRADES_PER_DAY = 8          # Must be > MAX_CONCURRENT to allow slot cycling
MAX_CONCURRENT_TRADES = 5
MAX_ENTRIES_PER_SYMBOL = 1      # Avoid doubling up on same stock

# =========================================================
# REWARD & MANAGEMENT
# =========================================================
TP1_REWARD_RATIO = 1.75         # Research-backed: 18% better avg R than 1.5

# =========================================================
# ENGINE TIMERS
# =========================================================
POLL_INTERVAL_SECONDS = 60       # Slow loop (scans for new entries)
FAST_POLL_INTERVAL_SECONDS = 5   # Fast loop (manages active trades)

ENTRY_START = time(9, 30)
ENTRY_END   = time(14, 30)   # consolidation breakout entry cutoff
FORCE_EXIT  = time(15, 15)

# =========================================================
# CONSOLIDATION BREAKOUT PARAMETERS
# =========================================================
CONSOLIDATION_CANDLES   = 5      # last 5 bars (includes yesterday early morning)
CONSOLIDATION_RANGE_PCT = 0.01   # box must be within 1%
ATR_SQUEEZE_RATIO       = 0.8    # ATR5 < 0.8 × ATR20
VWAP_PROXIMITY_PCT      = 0.007  # within 0.7% of VWAP
LIMIT_BEYOND_BOX_PCT    = 0.005  # LIMIT entry 0.5% beyond box edge

# =========================================================
# NIFTY 100 SCANNER PARAMETERS
# =========================================================
NIFTY100_SCAN_INTERVAL   = 900   # seconds — scan every 15 minutes
SCORE_ADD_THRESHOLD      = 70    # add to watchlist when score > 70
SCORE_REMOVE_THRESHOLD   = 50    # remove from watchlist when score < 50
