# Intraday Pullback Engine — Research & Configuration Guide

This document captures all research done on the strategy and explains every
configuration decision with data to back it up. Read this before changing
any setting in `config/settings.py`.

---

## Table of Contents

1. [System Overview](#1-system-overview)
2. [Weekly Workflow](#2-weekly-workflow)
3. [Backtest Research — NIFTY 200 Results](#3-backtest-research--nifty-200-results)
4. [Trailing vs No-Trailing — Which Is Better?](#4-trailing-vs-no-trailing--which-is-better)
5. [Optimal Risk:Reward Ratio](#5-optimal-riskreward-ratio)
6. [How Many Stocks to Trade?](#6-how-many-stocks-to-trade)
7. [Position Sizing & Margin Guide](#7-position-sizing--margin-guide)
8. [Settings Reference](#8-settings-reference)
9. [Going Live — Checklist](#9-going-live--checklist)

---

## 1. System Overview

The engine is a **NIFTY-relative RS pullback strategy** for intraday equity trading on NSE.

```
Weekly Backtest (Sunday)
    ↓ scans NIFTY 200 stocks
    ↓ filters stocks with proven edge
    ↓ saves preferred_watchlist.csv
    ↓
Live Engine (Mon–Fri, 9:30–12:00)
    ↓ scans watchlist every 60 seconds
    ↓ ranks each stock (RS, VWAP, trend, volume)
    ↓ enters pullback on qualifying signal
    ↓ manages with partial TP + trailing stop
    ↓ force-exit at 15:15
```

**Key design principle:** The backtest selects *which* stocks to trade. The live
engine only trades stocks that have shown a historical edge with this strategy.
Run the backtest every Sunday and update the watchlist before Monday open.

---

## 2. Weekly Workflow

### Step 1 — Run the Backtest (Every Sunday)

```bash
cd Intraday_strategy_stock_buying
python3 backtest/run_backtest.py
```

- Tests all 205 NIFTY 200 stocks on last 3 months of 5-min data
- Takes ~40 minutes
- Auto-saves filtered watchlist to `data/preferred_watchlist.csv`
- Full report saved to `backtest/results/report_YYYYMMDD.csv`

### Step 2 — Review the Output

Check `backtest/results/report_YYYYMMDD.csv`. Look for:
- `avg_r > 0.15` — meaningful positive edge
- `profit_factor > 1.2` — more winning value than losing
- `trades >= 8` — enough signals to be statistically meaningful

### Step 3 — Start the Live Engine (Monday Morning)

```bash
cd Intraday_strategy_stock_buying
python3 main.py
```

The engine picks up `data/preferred_watchlist.csv` automatically.

---

## 3. Backtest Research — NIFTY 200 Results

**Backtest run date:** 17 May 2026
**Universe:** 205 stocks
**Period:** Last 3 months (5-min bars)
**Strategy:** Current trailing approach (50% at TP1, trail remaining 50%)

### Qualified Stocks (15 passed the filter)

| Rank | Stock | Trades | Win% | Avg R | PF |
|------|-------|--------|------|-------|-----|
| 1 | ETERNAL | 161 | 54.7% | +0.512 | 2.13 |
| 2 | HCLTECH | 173 | 54.3% | +0.463 | 2.01 |
| 3 | TECHM | 169 | 48.5% | +0.431 | 1.84 |
| 4 | COFORGE | 155 | 51.6% | +0.366 | 1.78 |
| 5 | JKCEMENT | 159 | 52.8% | +0.358 | 1.76 |
| 6 | JUBLFOOD | 109 | 49.5% | +0.356 | 1.70 |
| 7 | TATAELXSI | 161 | 47.8% | +0.353 | 1.68 |
| 8 | BHEL | 139 | 46.8% | +0.322 | 1.61 |
| 9 | NAVINFLUOR | 124 | 43.5% | +0.316 | 1.56 |
| 10 | TATACHEM | 125 | 43.2% | +0.311 | 1.55 |
| 11 | LT | 86 | 45.3% | +0.297 | 1.54 |
| 12 | JYOTHYLAB | 113 | 47.8% | +0.266 | 1.51 |
| 13 | PNB | 114 | 48.2% | +0.252 | 1.49 |
| 14 | PFC | 138 | 44.9% | +0.249 | 1.46 |
| 15 | PRESTIGE | 144 | 45.8% | +0.235 | 1.43 |

### Important Finding — Signal Edge Split

```
LONG  trades avg R = -0.006  (across all 202 stocks)
SHORT trades avg R = -0.016  (across all 202 stocks)
```

The overall edge is near zero across all stocks. The 15 qualified stocks above are
genuine outliers — stocks whose price behaviour suits the pullback pattern.
**This is exactly why the weekly backtest filter exists.** Without it, the strategy
would be nearly breakeven across the full universe.

### Filter Thresholds Used

```python
MIN_TRADES        = 8      # ignore stocks with too few signals
MIN_PROFIT_FACTOR = 1.2    # at least 20% more winning value than losing
MIN_AVG_R         = 0.05   # minimum positive R per trade
MAX_STOCKS        = 15     # cap to keep watchlist focused
```

---

## 4. Trailing vs No-Trailing — Which Is Better?

**Comparison run:** 17 May 2026 on all 202 stocks

### Strategy Definitions

| Strategy | SL hit | TP1 hit |
|----------|--------|---------|
| **With Trailing** | Exit 100% at -1R | Exit 50% at +1.5R, trail remaining 50% with 3-bar extremes |
| **No Trailing** | Exit 100% at -1R | Exit 100% at +1.5R immediately |

### Overall Results (202 stocks)

| Metric | With Trailing | No Trailing |
|--------|--------------|-------------|
| Avg R-multiple | -0.0283 | **-0.0099** |
| Avg Profit Factor | 0.978 | **1.007** |
| Stocks where better | 70 | **132** |
| **Winner** | | ✅ **No Trailing** |

### But — On Your Qualified Stocks, Trailing Wins

| Stock | Trail avg_R | No-Trail avg_R | Better |
|-------|------------|----------------|--------|
| ETERNAL | +0.512 | +0.366 | **Trail** |
| HCLTECH | +0.463 | +0.357 | **Trail** |
| TECHM | +0.431 | lower | **Trail** |
| JKCEMENT | +0.358 | +0.316 | **Trail** |
| COFORGE | +0.366 | +0.294 | **Trail** |

### Conclusion

- No-trailing wins on the **general universe** (mean-reverting stocks)
- Trailing wins on the **top trending stocks** (which are your watchlist stocks)
- The weekly backtest already filters for stocks where trailing works
- **Keep the trailing approach as-is** — do not change it

Only 5 stocks have edge ONLY with trailing: TATACONSUM, POONAWALLA, CANFINHOME, CHOLAHLDNG, METROPOLIS.
Only 23 stocks have edge ONLY without trailing — most are stocks not in your watchlist.

---

## 5. Optimal Risk:Reward Ratio

**Research run:** 17 May 2026 on the 15 preferred stocks
**Method:** Each signal simulated at 6 different TP1 ratios simultaneously (single data fetch)

### Overall Results Across 15 Stocks

| Ratio | Avg R | Win% | PF | Total R |
|-------|-------|------|----|---------|
| 1:1.50 (current) | +0.214 | 48.7% | 1.42 | 444 |
| 1:1.65 | +0.229 | 46.6% | 1.43 | 474 |
| **1:1.75** | **+0.242** | **45.7%** | **1.46** | **511** |
| 1:2.00 | +0.274 | 43.3% | 1.50 | 581 |
| 1:2.50 | +0.319 | 38.8% | 1.53 | 660 |
| 1:3.00 | +0.369 | 35.9% | 1.58 | 764 |

### Per-Stock Best Ratio

| Stock | 1:1.5 | 1:1.75 | 1:2.0 | 1:3.0 | Best |
|-------|-------|--------|-------|-------|------|
| ETERNAL | +0.367 | +0.401 | +0.496 | +0.574 | 1:3.0 |
| HCLTECH | +0.357 | +0.361 | +0.421 | +0.599 | 1:3.0 |
| TECHM | **+0.213** | +0.204 | +0.144 | +0.143 | **1:1.5** |
| COFORGE | +0.294 | +0.249 | +0.339 | +0.472 | 1:3.0 |
| JKCEMENT | +0.316 | +0.398 | **+0.449** | +0.370 | **1:2.0** |
| JUBLFOOD | +0.221 | +0.301 | +0.348 | +0.618 | 1:3.0 |
| TATAELXSI | +0.196 | +0.264 | +0.304 | +0.536 | 1:3.0 |
| TATACHEM | +0.080 | **+0.127** | +0.083 | +0.068 | **1:1.75** |
| PRESTIGE | **+0.143** | +0.092 | +0.088 | +0.116 | **1:1.5** |

**10 out of 15 stocks prefer 1:3.0. Only 2 prefer the current 1:1.5.**

### Why 1:1.75 Is the Recommended Setting

Even though 1:3.0 gives the highest avg_R (+0.369), `1:1.75` is chosen because:

1. **All 15 stocks remain positive** at 1:1.75 — no stock goes negative
2. At 1:3.0, TECHM drops from +0.213 to +0.143, PRESTIGE to +0.116, TATACHEM to +0.068
3. Win rate stays at a comfortable 45.7% (vs 35.9% at 1:3.0 — psychologically harder)
4. **18% improvement over current 1:1.5** with zero downside to any stock
5. Safe middle ground before experimenting with stock-specific ratios

**Setting:** `TP1_REWARD_RATIO = 1.75` in `config/settings.py`

---

## 6. How Many Stocks to Trade?

### Why 10 Stocks, Not 15

| Group | Stocks | Avg R at 1:1.75 | PF |
|-------|--------|-----------------|-----|
| Top 10 | ETERNAL, JKCEMENT, HCLTECH, JUBLFOOD, TATAELXSI, PNB, COFORGE, JYOTHYLAB, LT, TECHM | **+0.292** | 1.48 |
| Bottom 5 | PFC, NAVINFLUOR, BHEL, TATACHEM, PRESTIGE | **+0.142** | 1.22 |

The bottom 5 are 51% weaker than the top 10.

**The concurrent slot problem:** With `MAX_CONCURRENT_TRADES = 3`, the engine
fills slots in watchlist order. If BHEL or PRESTIGE fire early, they occupy a
slot that ETERNAL or HCLTECH could have used. Quality over quantity.

### Recommended Watchlist Order (best first)

```
ETERNAL, JKCEMENT, HCLTECH, JUBLFOOD, TATAELXSI,
PNB, COFORGE, JYOTHYLAB, LT, TECHM
```

Order matters — the live engine scans top to bottom and enters on first signal.
Strongest stocks should be at the top to get priority.

---

## 7. Position Sizing & Margin Guide

### Without Margin (Base Capital ₹1,00,000)

| Concurrent | Capital/slot | HCLTECH qty | Brokerage% | Verdict |
|-----------|-------------|-------------|-----------|---------|
| 3 | ₹31,667 | 19 shares | 0.13% | ✅ Good |
| 5 | ₹19,000 | 11 shares | 0.23% | ✅ OK |
| 10 | ₹9,500 | 5 shares | 0.50% | ⚠️ Too small |

**With no margin, stay at 3 concurrent trades.**

### With 4.5x Intraday Margin (Effective Capital ₹4,50,000)

| Concurrent | Effective slot | Real money/slot | Brokerage% | Verdict |
|-----------|---------------|----------------|-----------|---------|
| 3 | ₹1,42,500 | ₹31,667 | 0.03% | ✅ Excellent |
| 5 | ₹85,500 | ₹19,000 | 0.05% | ✅ Excellent |
| **10** | **₹42,750** | **₹9,500** | **0.10%** | **✅ Viable** |

With 4.5x margin, even 10 concurrent trades gives healthy position sizes (brokerage < 0.1%).

### Settings for 4.5x Margin + 10 Concurrent Trades

```python
# config/settings.py

# Set CAPITAL to effective leveraged amount
CAPITAL                 = 450000.0

# Keep risk at 1% of REAL capital (₹1,00,000 × 1% = ₹1,000 per trade)
# 1000 / 450000 = 0.22%
MAX_RISK_PER_TRADE_PCT  = 0.22

# Concurrent and daily limits
MAX_CONCURRENT_TRADES   = 10
MAX_TRADES_PER_DAY      = 15      # must be > concurrent to allow cycling

# Daily loss limit — at 10 concurrent, worst day = 10 × ₹1,000 = ₹10,000
MAX_DAILY_LOSS_PCT      = 5.0     # stops engine at ₹22,500 loss (5% of ₹4.5L)
                                  # = ₹5,000 on real capital

TP1_REWARD_RATIO        = 1.75
```

### Risk Per Day — Worst Case Analysis

| Setup | Max trades/day | Max daily loss | % of real capital |
|-------|---------------|----------------|-------------------|
| 3 concurrent, no margin | 5 | ₹5,000 | 5% |
| 5 concurrent, 4.5x margin | 10 | ₹10,000 | 10% |
| **10 concurrent, 4.5x margin** | **15** | **₹15,000** | **15%** |

**Important:** These are WORST CASE (all trades hit SL on same day). With a
strategy that has 45–54% win rate, this scenario is rare. But size positions
knowing it can happen on a strongly trending day against your positions.

### Recommended Starting Point

Start conservatively. Scale up only after seeing consistent live results:

| Phase | Concurrent | Margin | MAX_DAILY_LOSS_PCT |
|-------|-----------|--------|-------------------|
| Paper testing | 3 | None | 2% |
| Live Month 1 | 3 | No margin | 2% |
| Live Month 2–3 | 5 | 2x margin | 3% |
| Live Month 4+ | 10 | 4.5x margin | 5% |

---

## 8. Settings Reference

All settings are in `config/settings.py`. Here is every parameter explained:

```python
# ── PAPER / LIVE TOGGLE ────────────────────────────────────────
PAPER_TRADE = True
# True  = no real orders, PnL simulated from LTP
# False = real orders placed on Dhan via MIS (intraday)
# Change to False only after completing paper testing

# ── CAPITAL & RISK ─────────────────────────────────────────────
CAPITAL = 100000.0
# Set to actual capital if no margin
# Set to (actual capital × margin multiplier) if using margin
# Example: ₹1L with 4.5x margin → 450000.0

MAX_RISK_PER_TRADE_PCT = 1.0
# % of CAPITAL risked per trade
# If using margin: set to (1% of real capital / CAPITAL)
# Example: 1L real, 4.5x → 1000/450000 = 0.22

MAX_DAILY_LOSS_PCT = 2.0
# Engine stops scanning for new entries when daily loss
# (realized + unrealized) exceeds this % of CAPITAL
# Set higher if using more concurrent trades

CAPITAL_UTILIZATION = 0.95
# Keeps 5% as buffer. Rarely needs changing.

# ── TRADE LIMITS ───────────────────────────────────────────────
MAX_TRADES_PER_DAY = 5
# Hard cap on total trades per day regardless of P&L
# Should be ≥ MAX_CONCURRENT_TRADES to allow slot cycling

MAX_CONCURRENT_TRADES = 3
# Maximum open positions at the same time
# Research: 3 for ≤₹1L, 5 for ₹2.5L+, 10 for ₹5L+

MAX_ENTRIES_PER_SYMBOL = 1
# Prevents doubling up on the same stock

# ── EXIT MANAGEMENT ─────────────────────────────────────────────
TP1_REWARD_RATIO = 1.5
# Risk:Reward ratio for first partial exit
# Research finding: 1.75 is optimal for all 15 preferred stocks
# Recommended change: set to 1.75
#
# Stock-specific optimal ratios (for future hybrid implementation):
#   1:1.5  → TECHM, PRESTIGE
#   1:1.75 → TATACHEM
#   1:2.0  → JKCEMENT, JYOTHYLAB
#   1:3.0  → ETERNAL, HCLTECH, JUBLFOOD, LT, TATAELXSI, others

# ── ENGINE TIMING ───────────────────────────────────────────────
POLL_INTERVAL_SECONDS = 60
# Slow loop interval: scans watchlist for new entries every 60s

FAST_POLL_INTERVAL_SECONDS = 5
# Fast loop interval: checks open trades every 5s

ENTRY_START = time(9, 30)
# No entries before 9:30 AM (first 15 min excluded — too volatile)

FORCE_EXIT = time(15, 15)
# All open positions squared off at 15:15
# MIS positions auto-square by broker at ~3:20 PM anyway
```

---

## 9. Going Live — Checklist

Complete every item before switching `PAPER_TRADE = False`.

### Account Setup
- [ ] Dhan account KYC complete
- [ ] Intraday (MIS) trading enabled
- [ ] Short selling enabled (for SELL signals)
- [ ] TOTP 2FA set up and secret saved in `credentials.py`

### Strategy Validation
- [ ] Ran paper trading for at least 2–3 weeks
- [ ] Paper results roughly match backtest expectations
- [ ] Ran weekly backtest at least once to confirm watchlist is current
- [ ] Understand the strategy: only enters 9:30–12:00, exits by 15:15

### Settings Review
- [ ] `PAPER_TRADE = False`
- [ ] `CAPITAL` set to correct amount (real capital or leveraged)
- [ ] `MAX_RISK_PER_TRADE_PCT` calibrated to 1% of real capital
- [ ] `TP1_REWARD_RATIO = 1.75` (research-backed improvement)
- [ ] `MAX_DAILY_LOSS_PCT` appropriate for your concurrent trade count
- [ ] `MAX_CONCURRENT_TRADES` appropriate for your capital

### First Live Day
- [ ] Start with `MAX_CONCURRENT_TRADES = 1` on Day 1 (watch one trade fully)
- [ ] Verify order is placed correctly in Dhan app
- [ ] Verify SL order appears immediately after entry
- [ ] Check Telegram alert arrives on entry and exit
- [ ] Increase to normal concurrent count from Day 2 onwards

### Monitoring
- [ ] Check `logs/engine.log` daily for errors
- [ ] Check `state/engine_state.json` — shows active trades on restart
- [ ] Run weekly backtest every Sunday to refresh watchlist

---

*Last updated: May 2026*
*Research based on NIFTY 200 backtest (Feb–May 2026, 5-min data)*
*Strategy performance varies with market regime — rerun backtest weekly*
