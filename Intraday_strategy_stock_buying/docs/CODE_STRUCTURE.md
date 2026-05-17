# Intraday Strategy: Code Architecture

The algorithmic engine is highly modular, drawing inspiration from institutional trading architectures. It cleanly decouples API communication, state persistence, mathematical modeling, and execution logic.

## Directory Structure

```text
Intraday_strategy/
├── main.py                   # The central orchestrator (Dual-Loop Engine)
├── config/
│   ├── settings.py           # Core variables (Risk %, Timers, Capacities)
│   └── credentials.py        # API Keys and Telegram Tokens
├── engine/
│   ├── execution.py          # Live/Paper trade placement, SL trailing, and management
│   ├── risk.py               # Position sizing and capital slotting math
│   ├── state.py              # In-memory and JSON tracking of Active Trades
│   └── strategy.py           # The mathematical 'brain' (Indicators, Signals, Scoring)
├── data/
│   └── preferred_watchlist.csv # The pool of stocks the system scans
├── state/
│   └── engine_state.json     # The physical state file for crash recovery
└── logs/
    └── engine.log            # Verbose execution logs
```

## How the Engine Works (`main.py`)

The main orchestrator runs an infinite `while True` loop divided into two asynchronous-style gears to respect API rate limits and optimize CPU cycles:

1. **The Fast Loop (5 seconds): Trade Management**
   - Iterates through the list of currently open trades.
   - Fetches the Live Traded Price (LTP) and evaluates if Stop Loss, TP1, or Trailing conditions are met.
   - Delegates the actual order manipulation to `execution.py`.
2. **The Slow Loop (60 seconds): Setup Scanning**
   - Fetches the 5-minute historical data for the broader market (`NIFTY`).
   - Iterates through the `preferred_watchlist.csv`.
   - Delegates DataFrame parsing to `strategy.py` to evaluate Relative Strength, VWAP pullbacks, and Volume expansions.
   - If a valid setup is found, it calculates position sizing via `risk.py` and hands the payload to `execution.py` to fire the initial entry limits and stop losses.

## The Modules

### 1. `engine/strategy.py`
This is the mathematical brain. It uses `pandas` and `talib` to compute the moving averages, standardizes DataFrames, and assigns numerical scores to setups (`rank_stock`). It handles both historical processing and live-session window processing (`prepare_live_df`).

### 2. `engine/execution.py`
Handles all interaction with the Broker API (`Dhan_Tradehull`).
- Validates Live vs Paper mode.
- Includes **Orphan Protection**: If a limit order fires but the stop-loss API call is rejected, this module instantly sends a market order to close the naked exposure.
- Includes **Ghost Order Protection**: When modifying stop losses, it asynchronously queues the new SL placement to the next tick to prevent the broker from rejecting the order due to "cancellation in progress".

### 3. `engine/risk.py`
Evaluates the requested entry and stop loss prices, and calculates a safe quantity based on:
1. Max allowed capital per slot.
2. The strict `1%` Account Risk limit.

### 4. `engine/state.py`
Manages the `EngineState` dataclass. Every entry, exit, or partial profit is mapped here. 
- Tracks `realized_daily_pnl` to enforce the global Max Daily Loss circuit breaker.
- Continuously dumps the state to `state/engine_state.json` ensuring that if the server reboots, the engine will instantly recognize and reconnect to its live trades without duplicating entries or abandoning positions.
