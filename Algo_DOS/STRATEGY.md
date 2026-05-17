# Algo DOS: Directional Option Selling Engine

## 1. Strategy Overview
**Algo DOS** is a robust, automated trend-following system designed for selling Options (Credit Spreads) on the NIFTY index. It is built to operate continuously, utilizing dynamic volatility filters to avoid entering trades at the exhaustions of massive market spikes.

**Primary Characteristics:**
*   **Timeframe:** 60-Minute (Hourly) Candles.
*   **Trade Type:** Credit Spreads (Bull Put Spreads & Bear Call Spreads).
*   **Execution:** Continuous looping with separate fast (trade management) and slow (signal polling) intervals.

---

## 2. Technical Indicators & Entry Logic

The strategy relies on a combination of Trend identification and Volatility-adjusted Breakouts. It requires the following conditions to be met simultaneously:

### A. The Trend Filter
*   **Indicator:** Supertrend (Period: 10, Multiplier: 3.0).
*   **Rule:** The algorithm will only look for Bullish entries if the Supertrend is positive, and Bearish entries if the Supertrend is negative.

### B. The Breakout Trigger
*   **Indicator:** N-Candle Lookback High/Low (`BREAKOUT_LOOKBACK = 5`).
*   **Rule:** The engine monitors the highest high and lowest low of the previous 5 hourly candles. A valid trigger occurs when the current candle closes above the 5-candle high (Bullish) or below the 5-candle low (Bearish).

### C. Over-Extension Filters (The "Exhaustion" Protections)
This is the most critical part of the strategy. It prevents the system from buying into a massive spike that is likely to mean-revert.
1.  **Minimum Volatility (`MIN_ATR = 50.0`):** The market must have sufficient movement. If the ATR is too low, the system skips the trade because premiums will be too poor to justify the risk.
2.  **Candle Range Limit (`MAX_CANDLE_RANGE_ATR = 1.5`):** The breakout candle itself cannot be unusually massive (greater than 1.5x the current ATR). If it is, the move is considered exhausted.
3.  **Extension Limit (`MAX_EXTENSION_ATR = 0.35`):** The closing price cannot stretch too far past the breakout line. It must be a tight, controlled close just past the breakout level.

---

## 3. Options Strike Selection (Execution)
The engine does not use rigid, hardcoded strike distances (e.g., ATM - 100). Instead, it dynamically queries the live Dhan Option Chain to find strikes based on **Delta**, which automatically adjusts to the VIX (Market Volatility).

*   **Bullish Entry (Bull Put Spread):**
    *   **Short Leg:** Sells the Put option closest to **25 Delta**.
    *   **Hedge Leg:** Buys the Put option closest to **10 Delta** for protection.
*   **Bearish Entry (Bear Call Spread):**
    *   **Short Leg:** Sells the Call option closest to **25 Delta**.
    *   **Hedge Leg:** Buys the Call option closest to **10 Delta**.

*Note on Execution:* The engine uses margin-efficient routing. It will always **buy the Hedge leg first** and **sell the Short leg second** to ensure Dhan applies the hedged-margin benefit before placing the short order.

---

## 4. Position Sizing & Risk Management
Sizing is strictly controlled to protect capital:
*   **Risk Per Trade:** Capped at `2.0%` of Total Capital (`RISK_PER_TRADE_PCT`).
*   **Broker Margin Cap:** Even if the risk model allows for 10 lots, the system calculates actual required broker margin (`45,000 per lot`). If you only have ₹1,00,000 available, it strictly caps your order to 2 lots to prevent broker rejection.

---

## 5. Exit Management
Once a trade is open, the engine enters a **Fast Loop (polling every 5 seconds)** to track real-time Option LTPs. It exits under three conditions:

1.  **Target Profit Hit:** The net credit decays by `65%` (Target).
2.  **Stop Loss Hit:** The net credit inflates by `85%` (Loss Cut).
3.  **Trend Reversal:** If the hourly Supertrend flips against the open position, the engine immediately flattens the trade to protect capital.

---

## 6. System Safety Mechanisms
*   **State Persistence:** The engine writes its active state to `state/engine_state.json`. If the server reboots or crashes, the system instantly recovers its memory on startup and resumes trailing open trades.
*   **Market Hours Sleeper:** Outside of **09:15 to 15:30**, the engine hibernates without making any API requests to prevent Dhan rate limits or IP bans.
*   **Orphaned Leg Rollback:** If the Short Leg order gets rejected by the exchange during entry, the engine automatically sells off the Hedge Leg to ensure you aren't left with naked, bleeding options.
