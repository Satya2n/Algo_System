# Documentation: `main.py`

## Overview
The orchestration script. It wires together the Config, Strategy, Execution, Risk, and State modules into a continuous, infinite loop.

## The Dual-Loop Architecture
1. **The Market Hours Sleeper:**
   Checks the system clock. If the time is outside `09:15` to `15:30`, the script goes to sleep (`time.sleep(60)`), saving CPU and API calls.
2. **The Fast Loop (Trade Management):**
   Runs every `5 seconds` (if an active trade exists). Directly calls `executor.manage_open_trade()` to immediately process Stop-Losses and Targets based on live tick data.
3. **The Slow Loop (Setup Discovery):**
   Runs every `60 seconds` (if no trade exists). Fetches the latest historical candle from Dhan and runs it through the `StrategyEngine` pipeline.

## Edge Cases Handled
- **API Rate Limiting:** By cleanly separating the 5-second fast loop (which only triggers when holding a live position) from the 60-second historical fetch loop, the script avoids spamming the Dhan servers and triggering "429 Too Many Requests" IP bans.
- **Graceful Shutdown:** Implements a `try/except KeyboardInterrupt` block. When you hit `Ctrl+C`, it catches the signal, saves the final `engine_state.json` to the disk, and logs a clean shutdown message rather than crashing violently.
