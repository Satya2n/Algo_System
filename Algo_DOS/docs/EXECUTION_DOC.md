# Documentation: `engine/execution.py`

## Overview
The "Muscle" of the algorithm. This file bridges the gap between the `StrategyEngine` telling us *what* to do, and actually executing those orders via the Dhan API in a margin-efficient manner.

## Core Mechanisms

### 1. Dynamic Strike Selection (`build_spread_plan`)
Unlike amateur systems that hardcode strike distances (e.g., Spot - 100), this engine dynamically parses the Live Option Chain.
- It finds the short leg closest to **25 Delta**.
- It finds the hedge leg closest to **10 Delta**.

### 2. Live Order Sequencing (`_live_enter`)
Indian Brokers calculate margin dynamically. If you try to sell a naked Put, Dhan blocks ₹1,00,000. 
- The engine explicitly **Buys the Hedge Leg First**.
- It waits for a successful fill.
- It then **Sells the Short Leg**, which now only requires ₹45,000 margin.

### 3. Continuous Management (`manage_open_trade`)
Once a trade is open, this function runs every 5 seconds.
- Fetches real-time LTP for both Option Legs.
- Calculates instantaneous PnL.
- Evaluates the `65% Target Profit` and `85% Stop Loss`.
- Enforces the `35% Cost-to-Cost Trailing Stop`.

## Edge Cases Handled
- **Orphaned Leg Rejection:** If the Hedge Leg buys successfully, but the Short Leg gets rejected by the exchange (due to sudden margin changes or exchange glitches), the engine has a `try/except` block that automatically fires a Market Sell order to dump the Hedge Leg. This ensures you are never stuck holding bleeding "protection" without the core short position.
- **Whipsaw Protection:** The trailing stop requires a definitive 35% decay before locking at breakeven, preventing gamma spikes from unnecessarily triggering tight trailing stops.
