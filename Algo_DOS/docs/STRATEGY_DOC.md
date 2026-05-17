# Documentation: `engine/strategy.py`

## Overview
This file contains the core algorithmic intelligence. It ingests historical OHLCV data from Dhan and appends technical indicators to generate actionable `BULLISH_ENTRY` or `BEARISH_ENTRY` signals.

## Core Logic
1. **Supertrend Filter:** Calculates Supertrend (10, 3.0). It acts as a macro-filter. The algorithm will never buy against the Supertrend.
2. **N-Candle Breakout:** Calculates the `rolling_max` and `rolling_min` of the last 5 candles. A breakout occurs if the current close breaches these levels.
3. **Volatility Filters (ATR):**
   - **Minimum Volatility:** Rejects setups if ATR < `MIN_ATR` (low premium environments).
   - **Exhaustion Spike:** Rejects the setup if the breakout candle itself is larger than 1.5x ATR. This prevents "buying the top" of a huge news spike.
   - **Extension Limit:** Ensures the breakout close is tight to the breakout line (< 0.35x ATR past the line).

## Edge Cases Handled
- **The "Late to the Party" Edge Case:** If Nifty gaps up 300 points at the open, technically it breaks the 5-candle high. A naive strategy would buy here. The `MAX_CANDLE_RANGE_ATR` and `MAX_EXTENSION_ATR` immediately reject this, recognizing that the move is already exhausted and a mean-reversion pullback is imminent.
