# Intraday Equity Consolidation Breakout Strategy

## 1. Core Concept

This system trades **intraday consolidation breakouts** in the Indian Equity Market (NSE). It dynamically screens all NIFTY 100 stocks every 15 minutes using a momentum score, builds a live watchlist, then monitors those stocks on the 5-minute timeframe for a specific price structure (tight consolidation near VWAP) and enters when price breaks out and confirms with a pullback candle.

---

## 2. Architecture — Two Separate Services

**Scanner Service (`scanner/nifty100_scanner_service.py`)**
- Runs independently every 15 minutes
- Fetches 15-minute OHLCV data for all 100 NIFTY 100 stocks
- Scores each stock using RS, Trend, Volume on the 15-min timeframe
- Stocks scoring > 70 with volume score >= 4 are added to `state/dynamic_watchlist.json`
- Sends Telegram alert when watchlist changes (add / remove / direction change)
- Shares the same Dhan session token as the main engine (no duplicate login)

**Main Engine (`main.py`)**
- Reads `dynamic_watchlist.json` every 60 seconds
- Runs a 3-state machine per stock (WATCHING → CONSOLIDATING → BREAKOUT)
- Manages open trades (SL / TP1) every 5 seconds
- Places entries on pullback candles after confirmed breakouts

---

## 3. Scoring (15-Minute Timeframe)

The scanner scores each stock out of 100 across 6 components:

| Component | Weight | What It Measures |
|---|---|---|
| Relative Strength vs NIFTY | 3.0× | Stock outperforming / underperforming NIFTY |
| VWAP Distance | 2.5× | Position relative to session equilibrium |
| Trend (EMA + Swing) | 2.0× | Directional momentum — EMA9/21, higher lows/highs |
| Volume (RVOL) | 1.5× | Participation vs 20-bar historical baseline |
| Gap | 0.5× | Opening gap from previous close |
| Volatility (ATR%) | 0.5× | Moderate volatility preferred |

**Threshold:** Long score OR Short score > 70 AND volume score >= 4 → added to watchlist

---

## 4. Entry Logic — 3-State Machine (5-Minute Timeframe)

### STATE 1 — WATCHING
Every 60 seconds, check 3 consolidation conditions on the last 5 completed 5-min candles:

1. **Tight Range:** `(box_high - box_low) / box_low ≤ 1%`
2. **ATR Squeeze:** `ATR5 < 0.8 × ATR20` — volatility contracting
3. **Directional VWAP Proximity:**
   - LONG: price must be 0–0.7% **above** VWAP
   - SHORT: price must be 0–0.7% **below** VWAP

All 3 pass → record the box (box_high, box_low), move to STATE 2.

*Note: Uses full historical data so at 9:30 AM yesterday's last 5 candles form the box, transitioning to today's candles naturally by 9:40 AM.*

### STATE 2 — CONSOLIDATING
Every 60 seconds, check if the last completed 5-min candle broke out:
- LONG: `close > box_high`
- SHORT: `close < box_low`

Breakout confirmed → record breakout candle timestamp, move to STATE 3.

Invalidation: if price closes on the **wrong side** of the box (LONG but close < box_low) → reset to STATE 1.

### STATE 3 — PULLBACK ENTRY WINDOW
Wait for the **next** completed 5-min candle after the breakout (detected by candle timestamp change).

On that candle, check:
1. Breakout still held: LONG close still > box_high / SHORT close still < box_low
2. Directional VWAP: 0–0.8% from VWAP (slightly wider than consolidation)

Both pass → place LIMIT entry order. If either fails → reset to STATE 1.

**Only one pullback candle is allowed.** If the next candle fails, the setup is invalidated.

---

## 5. Order Placement

| Parameter | Value |
|---|---|
| Entry order | LIMIT at box edge + 0.5% (box_high × 1.005 for LONG) |
| Stop Loss | Box opposite edge (box_low × 0.999 for LONG) — structural SL |
| TP1 | 1.75 × risk from actual fill price |
| Entry attempts | Single LIMIT attempt — cancel if pending after 2s |
| Exit | Single MARKET order — retries every 5s if session fails |

---

## 6. Scanner Removal Protection

Once a stock enters STATE 2 or STATE 3, it is **protected from scanner removal**. The engine tracks states in memory (`consolidation_map`). Even if the 15-min scanner drops a stock from the watchlist (e.g., score dropped during consolidation), the engine continues monitoring it until the setup resolves (breakout fires or invalidated).

---

## 7. Risk & Capacity

| Parameter | Value |
|---|---|
| Capital | ₹50,000 real × 4.5× margin = ₹2,25,000 effective |
| Risk per trade | 0.22% of effective capital (~₹495) |
| Max trades per day | 8 |
| Max concurrent trades | 5 |
| Max daily loss | 1.5% of starting capital (₹3,375) — checked using real LTP |
| Entry window | 9:30 AM – 2:30 PM |
| Force exit | 3:15 PM |

---

## 8. Telegram Alerts

| Event | Source | Method |
|---|---|---|
| Engine started | Main engine | Dhan session |
| Watchlist stock added | Scanner | Direct HTTP |
| Watchlist stock removed | Scanner | Direct HTTP |
| Watchlist summary | Scanner | Direct HTTP (every 15 min) |
| Trade entry filled | Main engine | Direct HTTP |
| SL hit | Main engine | Direct HTTP |
| TP1 hit | Main engine | Direct HTTP |
| Exit failed (urgent) | Main engine | Direct HTTP |
| Force exit failed (urgent) | Main engine | Direct HTTP |
| Engine error | Main engine | Direct HTTP |
