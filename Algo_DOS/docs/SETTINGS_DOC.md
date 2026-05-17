# Documentation: `config/settings.py`

## Overview
This file serves as the single source of truth for all configurable parameters in the Algo_DOS engine. It prevents "magic numbers" from being scattered across the codebase.

## Key Sections

### 1. Engine & Capital Settings
- `PAPER_TRADE`: A boolean flag. If `True`, the engine simulates trades locally without sending orders to Dhan. If `False`, it executes real money trades.
- `CAPITAL`: The total capital dedicated to the algorithm (e.g., ₹1,00,000).
- `RISK_PER_TRADE_PCT`: The maximum percentage of total capital risked on a single trade (e.g., `0.02` for 2%).

### 2. Option Spread Rules
- `ESTIMATED_MARGIN_PER_LOT`: Crucial for Indian markets. Set to `45000.0` to reflect actual SPAN + Exposure required by Dhan for a hedged credit spread.
- `TRAIL_TO_BREAKEVEN_PCT`: Set to `0.35`. If the option premium decays by 35%, the Stop Loss is permanently moved to the Entry Price.

### 3. Loop Timers
- `POLL_INTERVAL_SECONDS`: Set to `60`. The slow loop timer for fetching historical data and searching for new breakouts.
- `FAST_POLL_INTERVAL_SECONDS`: Set to `5`. The aggressive timer used when an active trade is open to check real-time LTPs for Stop Loss / Target triggers.

## Edge Cases Handled
- **Accidental Over-Leverage:** By explicitly defining `ESTIMATED_MARGIN_PER_LOT`, the system is protected against the risk manager requesting 10 lots when the broker account only has enough margin for 2.
