# Documentation: `engine/state.py`

## Overview
The memory persistence layer of the system. It handles reading and writing the `state/engine_state.json` file.

## Core Data Structures
1. **`ActiveTrade` Dataclass:**
   Stores all relevant data about an open position: `entry_price`, `order_id`, `qty`, `pnl`, and the critical `sl_moved_to_breakeven` boolean.
2. **`EngineState` Dataclass:**
   Stores the global state: `trade_taken_today`, `today_trade_count`, and the current timestamp.

## Edge Cases Handled
- **System Crashes & Reboots (The "Amnesia" Edge Case):** If the python script is killed via Ctrl+C, a server reboot, or a WiFi drop, all Python RAM memory is lost. By continuously syncing the `ActiveTrade` object to a local `.json` file, the engine instantly recovers its context upon restart.
- **Midnight Rollovers:** The `reset_daily_counters_if_new_day()` function checks if the system date has changed. If it crosses midnight, it resets `trade_taken_today = False`, ensuring the bot is allowed to trade again the next morning without requiring manual intervention.
