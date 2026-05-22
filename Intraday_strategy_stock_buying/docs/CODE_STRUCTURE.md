# Intraday Strategy: Code Architecture

## Directory Structure

```text
Intraday_strategy_stock_buying/
├── main.py                         # Main engine — 3-state consolidation machine
├── config/
│   ├── settings.py                 # All parameters (risk, timers, consolidation thresholds)
│   └── credentials.py              # API keys and Telegram tokens (never committed)
├── engine/
│   ├── execution.py                # Order placement, SL/TP monitoring, exit logic
│   ├── risk.py                     # Position sizing and daily capital management
│   ├── state.py                    # Active trade persistence (JSON + in-memory)
│   └── strategy.py                 # Indicators, scoring, consolidation/breakout functions
├── scanner/
│   ├── nifty100_scanner_service.py # Standalone 15-min NIFTY 100 scanner service
│   ├── nifty100_stocks.py          # List of 100 stocks to scan
│   └── algo-scanner.service        # systemd unit file for the scanner
├── state/
│   ├── engine_state.json           # Active trades + daily counters (crash recovery)
│   └── dynamic_watchlist.json      # Scanner output — stocks scoring > 70 today
└── logs/
    ├── engine.log                  # Main engine logs
    └── scanner.log                 # Scanner service logs
```

---

## Two Services, One Dhan Session

**`algo-scanner.service`** — Start first
- Authenticates with Dhan, saves session token to `Dependencies/token_today.txt`
- Scans all 100 NIFTY 100 stocks every 15 minutes using 15-min candles
- Writes `state/dynamic_watchlist.json`
- Sends Telegram alerts on watchlist changes

**`algo-intraday.service`** — Start second
- Reads existing token file — no fresh authentication (no session conflict)
- Reads `dynamic_watchlist.json` every 60 seconds
- Runs 3-state machine on watchlist stocks using 5-min candles
- Manages open trades every 5 seconds

---

## How the Engine Works (`main.py`)

### Fast Loop — every 5 seconds
Monitors open trades. Fetches LTP, checks SL/TP1, places MARKET exit if triggered.
Exit failure → trade stays in state, retried every 5 seconds until session recovers.

### Slow Loop — every 60 seconds
Runs the 3-state consolidation machine on every stock in the dynamic watchlist.

**STATE 1 — WATCHING**
Checks 3 conditions on last 5 completed 5-min candles:
1. Range ≤ 1% (tight box)
2. ATR5 < 0.8 × ATR20 (volatility squeeze)
3. Directional VWAP: LONG 0–0.7% above VWAP / SHORT 0–0.7% below VWAP

All 3 pass → record box, advance to STATE 2.

**STATE 2 — CONSOLIDATING**
Checks if the last completed 5-min candle closed beyond the box.
Breakout confirmed → record candle timestamp, advance to STATE 3.
Wrong-direction close → invalidate, reset to STATE 1.

**STATE 3 — BREAKOUT**
Waits for the NEXT completed 5-min candle (detected by timestamp change).
Checks: breakout still held + VWAP directional 0–0.8%.
Valid → place LIMIT entry at box edge + 0.5%.
Invalid → reset to STATE 1.

---

## The Modules

### `engine/strategy.py`
Mathematical core using `pandas` and `talib`.

Key functions:
- `rank_stock(stock_full, nifty_full)` — scores stock 0–100 on RS, VWAP, Trend, Volume, ATR, Gap
- `is_consolidating(stock_full, direction, ...)` — checks 3 consolidation conditions, directional VWAP
- `is_breakout(stock_full, box_high, box_low, direction)` — checks last completed candle vs box, returns timestamp
- `is_pullback_entry_valid(stock_full, box_high, box_low, direction, breakout_ts, ...)` — STATE 3 check

### `engine/execution.py`
All broker interaction via `Dhan_Tradehull`.

Key behaviours:
- Single LIMIT entry attempt — cancel if pending after 2s (prevents double-fill)
- Single MARKET exit attempt — returns `None` on failure (trade stays in state, retries every 5s)
- `square_off_all()` — only removes trade from state when order SUCCEEDS
- All alerts via direct HTTP to Telegram (no Dhan session dependency)
- `_exit_alert_sent` set prevents Telegram spam on repeated exit failures

### `engine/risk.py`
Position sizing: `qty = min(risk_amount / per_share_risk, capital_slot / price)`.
Daily starting capital locked at first entry of the day.
Unrealized PnL checked using real LTP (not `trade.pnl` which is 0 while open).

### `engine/state.py`
`ActiveTrade` dataclass + `StateManager` with JSON persistence.
State file resets automatically when date changes.
Atomic write (`tmp` → `replace`) prevents corruption on crash.

### `scanner/nifty100_scanner_service.py`
Standalone process — runs independently.
Uses 15-minute candles for scoring (resolves contradiction with 5-min consolidation conditions).
Volume gate: `vol_score >= 4` required before adding to watchlist.
Stocks in STATE 2/3 (in `consolidation_map`) are protected from scanner removal.

---

## Key Design Decisions

| Decision | Reason |
|---|---|
| Software SL (not exchange SL-M order) | Keeps SL levels invisible to market participants |
| 15-min scoring + 5-min entry | Different timeframes eliminate contradictions between scoring criteria and entry conditions |
| Directional VWAP (not absolute) | LONG must be ABOVE VWAP, SHORT must be BELOW VWAP |
| Pullback entry (not breakout candle) | Better price, tighter SL, avoids chasing the surge |
| Single LIMIT entry + cancel | Prevents double-fill from pending order + retry |
| Scanner as separate service | NIFTY 100 scan never blocks SL monitoring |
| Shared Dhan token | Prevents session conflict between scanner and engine |
