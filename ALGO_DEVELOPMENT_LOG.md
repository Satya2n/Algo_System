# Algo Development Log
## Algo_Setup — Issues, Fixes & Lessons Learned

**Purpose:** Reference document for all issues encountered, fixes applied, and
lessons learned while building live trading strategies. Use this when building
new algos to avoid repeating the same mistakes.

**Last updated:** May 2026

---

## Table of Contents

1. [Infrastructure Issues](#1-infrastructure-issues)
2. [Order Execution Issues](#2-order-execution-issues)
3. [State & Data Persistence Issues](#3-state--data-persistence-issues)
4. [Authentication Issues](#4-authentication-issues)
5. [Strategy Logic Issues](#5-strategy-logic-issues)
6. [Backtest Issues](#6-backtest-issues)
7. [Deployment Checklist](#7-deployment-checklist)
8. [Standard Architecture Patterns](#8-standard-architecture-patterns)
9. [Change Log](#9-change-log)

---

## 1. Infrastructure Issues

---

### 1.1 EC2 Timezone Wrong — Engine Outside Market Hours All Day

**Date:** 19 May 2026
**Strategy:** Intraday_strategy_stock_buying

**Symptom:**
Engine logs showed "Outside market hours. Sleeping..." at 9:32 AM IST.
Strategy never started scanning despite market being open.

**Root Cause:**
EC2 defaulted to UTC timezone. `datetime.datetime.now()` returned UTC time.
`ENTRY_START = time(9, 30)` compared against UTC (3:32 AM) instead of IST (9:32 AM).

**Fix:**
```bash
sudo timedatectl set-timezone Asia/Kolkata
sudo systemctl restart algo-intraday
```

**Lesson:**
Always set EC2 timezone to IST immediately after launch.
```bash
# Add to EC2 setup steps
sudo timedatectl set-timezone Asia/Kolkata
timedatectl  # verify shows IST
```

---

### 1.2 Daily State Not Resetting — Engine Blocked All Day

**Date:** 19 May 2026
**Strategy:** Intraday_strategy_stock_buying

**Symptom:**
Next morning, engine had `trade_count_today = 8` (yesterday's count).
`MAX_TRADES_PER_DAY = 8` — engine immediately blocked, no new trades all day.

**Root Cause:**
`StateManager.load_state()` only runs at startup. If the service runs 24/7
without restart, the in-memory state carries over to the next day.
`reset_daily_counters_if_new_day()` exists but only triggers on a new startup.

**Fix:**
Added daily restart cron at 8:00 AM IST (Mon-Fri):
```bash
# 8:00 AM IST = 2:30 AM UTC
30 2 * * 1-5 sudo systemctl restart algo-intraday
35 2 * * 1-5 sudo systemctl restart algo-dos
```

**Lesson:**
Any strategy running 24/7 needs a daily restart before market open.
Alternatively: add a midnight date-check in the main loop to reset counters.

---

### 1.3 Python 3.12 Not Available on Ubuntu 26.04

**Date:** May 2026
**Context:** EC2 Setup

**Symptom:**
`sudo apt install python3.12` failed. Deadsnakes PPA doesn't support Ubuntu 26.04.

**Fix:**
Use pyenv with swap space:
```bash
# Add swap (required for compilation)
sudo fallocate -l 2G /swapfile
sudo chmod 600 /swapfile && sudo mkswap /swapfile && sudo swapon /swapfile

# Install pyenv
curl https://pyenv.run | bash
# Add to .bashrc, source it

# Install Python build dependencies first
sudo apt install -y libssl-dev zlib1g-dev libbz2-dev libreadline-dev \
    libsqlite3-dev libncursesw5-dev xz-utils tk-dev libxml2-dev \
    libxmlsec1-dev libffi-dev liblzma-dev

# Set temp dir to avoid /tmp space issues
export TMPDIR=/home/ubuntu/tmp && mkdir -p $TMPDIR
pyenv install 3.12
pyenv global 3.12
```

**Lesson:**
Ubuntu 26.04 ships Python 3.13. Always use pyenv for specific Python versions.
Deadsnakes PPA lags behind new Ubuntu releases by 6-12 months.

---

### 1.4 Log File Permission Denied on EC2

**Date:** May 2026
**Strategy:** Intraday_strategy_stock_buying

**Symptom:**
```
PermissionError: [Errno 13] Permission denied: 'logs/engine.log'
```
Service crashed on every restart.

**Root Cause:**
`mkdir -p logs/` created the folder as root (via sudo). Systemd runs service
as `ubuntu` user which couldn't write to root-owned folder.

**Fix:**
```bash
sudo chown -R ubuntu:ubuntu ~/algo/Algo_System/Intraday_strategy_stock_buying/logs
sudo systemctl restart algo-intraday
```

**Lesson:**
When creating log directories via systemd service file, ensure `User=ubuntu`
matches the owner of the log directory. Or create directories before starting
service as the correct user.

---

## 2. Order Execution Issues

---

### 2.1 Stop Loss Order Rejected — DH-906 Tick Size Error

**Date:** 19 May 2026
**Strategy:** Intraday_strategy_stock_buying
**Stock:** JKCEMENT

**Symptom:**
```
CRITICAL: [JKCEMENT] SL ORDER REJECTED!
error_code: DH-906
error_message: The order price is not multiple of the tick size
```

**Root Cause:**
Limit price rounding used `round(price * 20) / 20` = ₹0.05 precision.
Some stocks (e.g., JKCEMENT at ₹5,500+) have tick size ≠ ₹0.05.
Also, using STOPMARKET order type for SL — Dhan's handling was inconsistent.

**Fix 1 — Tick size from instrument file:**
```python
def _get_tick_size(self, symbol: str) -> float:
    try:
        inst = getattr(self.tsl, "instrument_df", None)
        if inst is not None:
            row = inst[inst["SEM_TRADING_SYMBOL"] == symbol]
            if not row.empty:
                return float(row.iloc[0].get("SEM_TICK_SIZE", 0.05))
    except Exception:
        pass
    return 0.05

def _round_to_tick(self, price: float, tick: float) -> float:
    return round(round(price / tick) * tick, 4)
```

**Fix 2 — Switch to software SL (monitor LTP, exit with MARKET order):**
Removed exchange-side STOPMARKET orders entirely.
Engine checks LTP every 5 seconds; if SL breached, places MARKET exit.

**Lesson:**
- Never assume ₹0.05 tick size for all NSE stocks
- Always fetch tick size from instrument_df before placing orders
- Software SL (LTP monitoring + MARKET exit) is more reliable than exchange stop orders for equity MIS

---

### 2.2 Orphan Trade — Entry Rejected but SL Exit Executed

**Date:** 19 May 2026
**Strategy:** Intraday_strategy_stock_buying
**Stock:** JKCEMENT

**Symptom:**
Entry order was rejected (tick size). Engine added trade to state anyway using
theoretical price. Later, SL triggered → MARKET SELL executed → naked short
position created with no corresponding BUY.

**Root Cause:**
`get_executed_price()` returned None (order not filled), but code fell back to
theoretical price and added trade to state:
```python
if not actual_entry_price:
    actual_entry_price = entry_price  # BUG: assumes order filled
```

**Fix:**
```python
# Verify order actually filled before adding trade
pytime.sleep(1.5)
order_status = self.tsl.get_order_status(orderid=entry_orderid)
if str(order_status).upper() in {"REJECTED", "CANCELLED", "FAILED"}:
    self._send_raw_alert(f"❌ ORDER REJECTED — {symbol}\nStatus: {order_status}")
    return None

actual_entry_price = self.tsl.get_executed_price(orderid=entry_orderid)
if not actual_entry_price:
    self._send_raw_alert(f"⚠️ ORDER NOT CONFIRMED — {symbol}\nNo fill. Trade NOT added.")
    return None
```

**Lesson:**
ALWAYS verify order fill before adding trade to state.
Never use theoretical price as fallback for live execution.
Send immediate Telegram alert on any order rejection.

---

### 2.3 Re-Entry on Same Stock — Multiple Trades Same Symbol Same Day

**Date:** 19 May 2026
**Strategy:** Intraday_strategy_stock_buying
**Stock:** BHEL (entered 3 times)

**Symptom:**
After TP1 exit, the same stock signal fired again within minutes.
Engine re-entered BHEL 3 times in the same day, burning through daily trade slots.

**Root Cause:**
`MAX_ENTRIES_PER_SYMBOL = 1` was defined in settings but never enforced in code.
Check was: `if state_manager.get_trade(sym): continue` — only blocks if trade
is currently OPEN, not if it was traded earlier today.

**Fix:**
Added `traded_symbols_today: list` to `EngineState`:
```python
# In state.py
traded_symbols_today: list = None

def add_trade(self, trade):
    ...
    if trade.symbol not in self.state.traded_symbols_today:
        self.state.traded_symbols_today.append(trade.symbol)

def has_traded_today(self, symbol: str) -> bool:
    return symbol in self.state.traded_symbols_today

# In main.py
if state_manager.has_traded_today(sym):
    continue  # skip — already traded today
```

**Lesson:**
Define AND enforce limits. Settings constants do nothing unless checked in code.
Track traded symbols separately from active trades.

---

## 3. State & Data Persistence Issues

---

### 3.1 State File Corrupted on Crash

**Issue:** Writing JSON to file directly — if process killed mid-write, file
is truncated and unreadable. Engine crashes on next startup.

**Fix — Atomic writes (write to .tmp then rename):**
```python
def save_state(self):
    tmp_path = self.state_file + ".tmp"
    with open(tmp_path, 'w') as f:
        json.dump(data, f, indent=4)
    os.replace(tmp_path, self.state_file)  # atomic on Linux/macOS
```

**Lesson:**
Always use atomic writes for state files. `os.replace()` is atomic on POSIX.

---

### 3.2 `entry_spot` Lost After Restart

**Date:** May 2026
**Strategy:** Algo_DOS

**Symptom:**
After engine restart with open carry-forward trade, paper PnL calculations
were wrong. `_paper_current_credit` used wrong baseline spot price.

**Root Cause:**
`entry_spot` was stored via `setattr(trade, "entry_spot", ...)` — not a proper
dataclass field — so it was never serialized to `engine_state.json`.
After restart, `entry_spot` defaulted to `entry_price` (option credit, not NIFTY spot).

**Fix:**
Added `entry_spot: float = 0.0` as a proper field in `ActiveTrade` dataclass.

**Lesson:**
Never use `setattr()` to store trade data. Only dataclass fields are serialized.
Every piece of data needed after restart must be in the dataclass.

---

### 3.3 Credentials Deleted from EC2 by git pull

**Date:** May 2026

**Symptom:**
After removing `credentials.py` from git tracking and pushing, `git pull` on EC2
also deleted the file from disk. Both services crashed on next restart.

**Root Cause:**
`git rm --cached credentials.py` removes from git index locally. When pushed
and pulled on EC2, git applies the deletion to EC2's working directory too.

**Fix:**
Manually recreated `credentials.py` on EC2:
```bash
cat > ~/algo/.../config/credentials.py << 'EOF'
DHAN_CLIENT_CODE = "..."
...
EOF
```

**Lesson:**
When removing a file from git tracking, the EC2 pull will DELETE the file
from disk. Always recreate it on EC2 immediately after.
Add to `.gitignore` BEFORE removing from tracking.

---

### 3.4 Shared Dhan Session Conflict — DH-906 Ping-Pong

**Date:** 20 May 2026 | **Strategy:** Both

**Symptom:**
Both services kept invalidating each other's session every 60s. Engine
went blind mid-session — silent failure, no Telegram alert.

**Root Cause:**
Each service had its own `Dependencies/token_YYYY-MM-DD.txt`. Every TOTP
login creates a new server session, killing the previous one.

**Fix — Shared symlink:**
```bash
mkdir -p ~/algo/Algo_System/shared_deps/log_files
ln -s ~/algo/Algo_System/shared_deps Intraday.../Dependencies
ln -s ~/algo/Algo_System/shared_deps Algo_DOS/Dependencies
```
Both services now share one token. One session. Zero conflict.

**Lesson:** Two services on one Dhan account must share one token file.
Stagger restarts 5 minutes apart so the first service establishes the session.

---

## 4. Authentication Issues

---

### 4.1 Dhan Session Invalidated — DH-901 All Day

**Date:** 19 May 2026
**Strategy:** Algo_DOS

**Symptom:**
Algo_DOS logged in successfully at startup but all subsequent API calls
returned `DH-901: Invalid Authentication`. Engine scanned nothing all day.
No Telegram alert was sent — silent failure.

**Root Cause:**
Both Intraday and Algo_DOS share the same Dhan account.
When Intraday restarted at 9:35 AM and generated a fresh TOTP login,
Dhan invalidated Algo_DOS's existing session.
The cached token became stale but the engine didn't detect or handle this.

**Fix 1 — Auto-reconnect:**
```python
# In Algo_DOS main.py slow loop
nifty_df = tsl.get_historical_data(...)
if nifty_df is None or nifty_df.empty:
    logger.warning("Data fetch empty. Attempting reconnect...")
    try:
        tsl = connect_tradehull(logger)
        telegram.send("⚠️ Algo_DOS: Reconnected to Dhan.")
    except Exception as e:
        telegram.send(f"❌ Algo_DOS: Reconnect FAILED. Not scanning!\n{e}")
```

**Fix 2 — Stagger restarts by 5 minutes:**
```
08:00 AM IST → Intraday restarts
08:05 AM IST → Algo_DOS restarts (after Intraday token saved)
```

**Lesson:**
- Two services sharing one Dhan account will conflict on login
- Always stagger logins by 5+ minutes
- Never silently ignore empty API responses — always attempt reconnect
- Send Telegram alert on ANY authentication failure

---

### 4.2 TOTP Secret Exposed in Git

**Date:** May 2026

**Issue:**
`credentials.py` was committed to GitHub with real values:
- DHAN_PIN
- DHAN_TOTP_SECRET (permanent — never expires)
- Telegram bot tokens

**Fix:**
```bash
# Add to .gitignore
echo "**/config/credentials.py" >> .gitignore

# Remove from git tracking (keeps file on disk)
git rm --cached Algo_DOS/config/credentials.py
git rm --cached Intraday_strategy_stock_buying/config/credentials.py

# Create example file
# credentials.example.py with placeholder values

# Recreate on EC2 manually after pull
```

**Lesson:**
Add `credentials.py` to `.gitignore` BEFORE writing any real values.
Create `credentials.example.py` as a template from day one.
If exposed: rotate TOTP secret in broker account immediately (TOTP is permanent).

---

## 5. Strategy Logic Issues

---

### 5.1 Slippage Not Tracked — No Visibility on Execution Quality

**Date:** May 2026

**Issue:**
No tracking of difference between signal price (bar close) and actual
executed price. JKCEMENT showed ₹54 slippage (1%) which was invisible.

**Fix:**
```python
# After getting actual_entry_price
slippage_pts = actual_entry_price - entry_price  # for BUY
slippage_pct = (slippage_pts / entry_price) * 100
slippage_rs  = slippage_pts * qty

self.logger.info(f"[{symbol}] Slippage: {slippage_pts:+.2f}pts ({slippage_pct:+.3f}%)")

if abs(slippage_pct) > 0.3:
    self.logger.warning(f"[{symbol}] HIGH SLIPPAGE — low liquidity suspected")
```

**Lesson:**
Always track slippage. Include it in Telegram entry alert.
Flag anything >0.3% as high slippage for review.

---

### 5.2 TP1 Not Recalculated After Actual Fill — Wrong RR on Every Trade

**Date:** 20 May 2026 | **Strategy:** Intraday

**Symptom:**
JYOTHYLAB SELL — RR dropped from intended 1:1.75 to 1:0.92. Trade was
entered with a negative RR without knowing it.

**Root Cause:**
TP1 is calculated from the signal bar close price (before fill).
When actual fill differs due to slippage:
- SELL trade filled lower → distance to SL increases → risk increases
- Distance to TP1 (calculated from signal price) decreases → reward decreases
- For JYOTHYLAB: signal ₹211.90 → fill ₹211.70 → RR became 0.61:0.66 = 0.92

**Fix:**
```python
# After getting actual fill, recalculate TP1 to maintain 1:1.75
actual_risk = abs(actual_entry_price - sl_price)
if side == "BUY":
    tp1 = actual_entry_price + (cfg.TP1_REWARD_RATIO * actual_risk)
else:
    tp1 = actual_entry_price - (cfg.TP1_REWARD_RATIO * actual_risk)
```

**Lesson:** Always calculate TP1 from actual fill price, not signal price.
Slippage impacts RR doubly on SELL trades (moves fill away from TP, closer to SL).

---

### 5.3 Volume Score Used Session-Only Bars — Unreliable Early Morning

**Date:** 20-21 May 2026 | **Strategy:** Intraday

**Symptom:**
Volume filter (score < 4 = skip) was unreliable at 09:30-10:00 AM.
Opening rush volume inflated the session average → normal bars scored 1/10
(false skip). Also allowed low-conviction moves to pass in some cases.

**Root Cause:**
`score_volume()` used session-only bars for the average:
- At 09:35 with 4 bars: average includes 09:15 high-volume opening bar
- Opening bar volume is 2-3x normal due to overnight pent-up orders
- Inflated average makes subsequent bars look "low volume" even when they're not

**Fix:**
```python
def score_volume(df, stock_full=None):
    current_vol = df["volume"].iloc[-1]
    if stock_full is not None and len(stock_full) >= 21:
        # Use full 20-bar history (includes yesterday) — stable baseline
        avg_vol = stock_full["volume"].iloc[-21:-1].mean()
    else:
        # Fallback: session-only
        avg_vol = df["volume"].tail(21).iloc[:-1].mean()
    rvol = current_vol / avg_vol
    ...
```
`rank_stock()` updated to pass `stock_full` to `score_volume()`.

**Backtest improvement (vs session-only):**
- Win rate: 47.8% → 51.9% (+4.1%)
- Avg R: +0.461 → +0.543 (+18%)
- Profit Factor: 1.90 → 2.15 (+0.25)

**Lesson:** Volume baseline must use stable historical data, not just today's
opening bars. Session-only average is unreliable for the first 30-45 minutes.

---

### 5.4 Score Threshold at Noise Boundary — SKIP Signals Firing

**Date:** 20 May 2026 | **Strategy:** Intraday

**Symptom:**
4 of 7 trades today showed SKIP (score < 70) when re-analysed post-market,
yet the live engine fired them. PFC (69.1), TATAELXSI (67.8),
MOTHERSON (64.6), COFORGE (52.6) all traded.

**Root Cause:**
Live Dhan data (real-time) differs slightly from historical data (finalized):
1. Intraday bars finalize after bar close — values can shift 0.1-0.5%
2. EMA calculations differ with full-day data vs 3-months-to-signal data
3. RVOL lookback window changes as session bars accumulate
These differences cause ±2-3 point fluctuations in score around the threshold.
A score of 70 live can appear as 67-73 post-market.

**Fix:**
Raised threshold 70 → 72 to create a 2-point buffer above the noise zone.

**Lesson:** Set signal thresholds above the noise level of the scoring system.
If live data vs finalized data causes ±3 point swings, the threshold must be
at least 3 points above the minimum acceptable quality level.

---

### 5.5 Gap-Down Regime — False SHORT Signals in First 45 Minutes

**Date:** 20 May 2026 | **Strategy:** Intraday

**Symptom:**
NIFTY gapped down -0.63% then recovered all day. Strategy fired 4 SHORT
signals (PFC, JYOTHYLAB, JUBLFOOD, TATAELXSI) while NIFTY was rising.
All 4 lost.

**Root Cause:**
On gap-down recovery days, ALL stocks start below yesterday's close.
RS calculation uses session return (from today's open), so stocks that
gapped down MORE than NIFTY look "weak" even if both are recovering.
The strategy sees negative RS → SHORT signal → but market is going UP.

**Suggested Fix (not yet implemented):**
```python
# In main.py slow loop, before scanning
gap_pct = (nifty_open - nifty_prev_close) / nifty_prev_close * 100
nifty_recovering = nifty_session_return > 0

if gap_pct < -0.3 and nifty_recovering:
    # Skip SHORT signals for first 45 minutes
    if current_time < time(10, 0):
        if signal == "ENTER_SHORT":
            continue
```

**Lesson:** Always check NIFTY regime before allowing directional signals.
Gap-down + recovery days create false SHORT signals in the first 30-45 minutes.

---

### 5.6 Extension Filter Missing — Entering After Surge

**Date:** 20 May 2026 | **Strategy:** Intraday

**Symptom:**
MOTHERSON +1.47% from open, COFORGE +2.27% from open at time of entry.
Both reversed immediately after entry. Strategy entered at the TOP of
the initial gap-recovery surge, not a genuine pullback.

**Suggested Fix (not yet implemented):**
```python
# Before placing entry
session_return = (latest["close"] - session_open) / session_open * 100
if abs(session_return) > 1.0:
    continue  # already extended — real pullback entries don't happen here
```

**Lesson:** Pullback strategy requires the stock to actually PULL BACK.
If it's already moved >1% from open, you're entering on extension, not pullback.

---

### 5.2 Liquidity Check Missing — Low Liquidity Stock Entered

**Date:** May 2026
**Stock:** JKCEMENT

**Issue:**
JKCEMENT at ₹5,464 had thin intraday liquidity at 10:27 AM.
Entry order rejected due to tick size + liquidity mismatch.
No pre-entry liquidity check existed.

**Fix (soft warning, does not block):**
```python
# Before placing order in main.py
avg_bar_value = (live_df["close"] * live_df["volume"]).mean()
position_value = qty * entry
impact_pct = (position_value / avg_bar_value) * 100

if impact_pct > 2.0:
    logger.warning(f"[{sym}] LOW LIQUIDITY: position = {impact_pct:.1f}% of avg bar")
```

**Lesson:**
Use position impact % (position_value / avg_bar_value) not raw volume.
Raw volume is price-blind. Impact % scales correctly for any stock price.
Keep as warning, not hard block — strategy decides, engine warns.

---

## 6. Backtest Issues

---

### 6.1 Backtest TP1 Simulation Inconsistent with Live Engine

**Date:** May 2026

**Issue:**
Backtest used `TP1_RR = 1.5` but live engine was changed to `TP1_REWARD_RATIO = 1.75`.
Backtest results (stock selection, metrics) were based on 1.5R but live traded at 1.75R.

**Fix:**
Synced both to 1.75:
```python
# run_backtest.py
TP1_RR = 1.75  # must match config/settings.py TP1_REWARD_RATIO
```

**Lesson:**
Backtest simulation parameters must EXACTLY match live engine parameters.
Review all constants when changing strategy settings — backtest and live.

---

### 6.2 Backtest Did Not Account for Trade Held Across Day Boundary

**Date:** May 2026

**Issue:**
`simulate_partial_trail_trade()` in notebook didn't stop at 15:15.
Trailing could continue overnight in simulation, giving unrealistic P&L.

**Fix:**
Added force exit at 15:15 in simulation:
```python
entry_date = _naive(stock_df.index[entry_idx]).date()
for j in range(len(future)):
    bar_ts = _naive(future.index[j])
    if bar_ts.date() != entry_date or is_force_exit(bar_ts):
        # exit at close of this bar
        break
```

**Lesson:**
Always enforce market hours in backtest simulation.
Backtest must mirror live engine behavior exactly — if live exits at 15:15,
simulation must too.

---

### 6.3 Dhan API Quota Exceeded During Backtest

**Date:** May 2026

**Issue:**
`get_expired_option_data` hit a per-session limit (~3 calls).
All subsequent calls returned empty DataFrame. No error raised.

**Fix:**
Use bulk fetch (one call per strike for full date range) + disk cache:
```python
cache_file = f"cache/NIFTY_{strike}_{option_type}.csv"
if os.path.exists(cache_file):
    return pd.read_csv(cache_file, parse_dates=["timestamp"])

raw = tsl.get_expired_option_data(..., from_date=start, to_date=end)
df.to_csv(cache_file, index=False)  # cache to disk
```

**Lesson:**
Cache API results to disk. Never re-fetch what you already have.
Assume any Dhan API has per-session or per-day call limits.

---

## 7. Deployment Checklist

Use this before deploying any new strategy to EC2.

### Pre-Deployment

- [ ] `credentials.py` in `.gitignore` — never committed
- [ ] `credentials.example.py` created as template
- [ ] All sensitive paths in `.gitignore` (tokens, logs, state, instrument CSVs)
- [ ] `PAPER_TRADE = True` verified before first run
- [ ] Test import: `python3 -c "from engine.execution import ExecutionEngine; print('OK')"`

### EC2 Setup

- [ ] Timezone set to IST: `sudo timedatectl set-timezone Asia/Kolkata`
- [ ] Swap space added (for compilation): `sudo fallocate -l 2G /swapfile`
- [ ] Python 3.12 via pyenv
- [ ] TA-Lib compiled and installed
- [ ] Virtual environment created, packages installed
- [ ] `credentials.py` created manually on EC2 (never from git)
- [ ] Log directories created with correct permissions: `sudo chown -R ubuntu:ubuntu logs/`

### Systemd Service

- [ ] `WorkingDirectory` absolute path correct
- [ ] `User=ubuntu` set
- [ ] Log directory exists before starting service
- [ ] `systemctl enable <service>` — auto-start on reboot
- [ ] `systemctl status <service>` — shows `active (running)`

### Daily Operations Cron

```bash
# 8:00 AM IST restart (Mon-Fri)
30 2 * * 1-5 sudo systemctl restart algo-intraday
35 2 * * 1-5 sudo systemctl restart algo-dos   # 5 min gap — avoid TOTP conflict

# Weekly backtest (Sunday 8 PM IST)
30 14 * * 0 cd ~/algo/Algo_System && python3 Intraday_strategy_stock_buying/backtest/run_backtest.py
```

### Paper Trading Validation (minimum 2 weeks)

- [ ] Startup Telegram alert received
- [ ] Entry alert received with correct: symbol, side, qty, entry, SL, TP1
- [ ] SL exit alert received when SL breached
- [ ] TP1 exit alert received when target hit
- [ ] Force exit at 15:15 works
- [ ] Daily state resets correctly next morning (trade_count = 0)
- [ ] No re-entry on same stock same day
- [ ] Order rejection shows Telegram alert (not silent)
- [ ] Slippage shown on every entry

### Going Live

- [ ] `PAPER_TRADE = False`
- [ ] Capital set correctly (with or without margin)
- [ ] Risk per trade calibrated to real capital
- [ ] Dhan account: MIS enabled, short selling enabled
- [ ] First live trade: watch Dhan app to verify order + SL both appear
- [ ] Keep Dhan app open on phone for first 3 days

---

## 8. Standard Architecture Patterns

### Every Strategy Should Have

```
strategy_name/
├── main.py                    ← orchestration loop
├── config/
│   ├── __init__.py
│   ├── settings.py            ← all tuneable parameters
│   └── credentials.example.py← template (real file in .gitignore)
├── engine/
│   ├── __init__.py
│   ├── state.py               ← dataclass + JSON persistence
│   ├── risk.py                ← position sizing
│   ├── execution.py           ← order placement + management
│   └── strategy.py            ← signal generation
├── data/
│   └── watchlist.csv          ← symbols to trade (if applicable)
├── state/
│   └── engine_state.json      ← runtime state (in .gitignore)
├── logs/
│   └── engine.log             ← in .gitignore
└── docs/
    └── STRATEGY_OVERVIEW.md
```

### Mandatory Patterns

**1. Atomic state writes:**
```python
tmp = state_file + ".tmp"
with open(tmp, 'w') as f:
    json.dump(data, f)
os.replace(tmp, state_file)
```

**2. Verify order fill before adding to state:**
```python
order_id = place_order(...)
if not order_id: return None
sleep(1.5)
if get_order_status(order_id) in {"REJECTED", "CANCELLED"}: return None
fill_price = get_executed_price(order_id)
if not fill_price: return None  # never use theoretical as fallback
```

**3. All trade data in dataclass (not setattr):**
```python
@dataclass
class ActiveTrade:
    entry_spot: float = 0.0   # always explicit fields
    entry_price: float = 0.0
    # never: setattr(trade, "entry_spot", x)
```

**4. Two-bot Telegram setup:**
```
Strategy A → Bot token A (dedicated, clear which strategy)
Strategy B → Bot token B
```

**5. Soft vs Hard limits:**
```
Liquidity check → WARNING (log + alert, don't block)
Daily loss limit → HARD STOP (no new entries)
Trade count limit → HARD STOP
Risk per trade → HARD CAP (qty calculation)
```

---

## 9. Change Log

| Date | Strategy | Change | Reason |
|------|----------|--------|--------|
| May 2026 | Both | Added `.gitignore` — removed `.venv`, `.DS_Store`, logs | Cleanup before first commit |
| May 2026 | Both | Removed credentials from git tracking | Security — tokens were exposed |
| May 2026 | Intraday | Added TOTP login fallback | Access token expires daily |
| May 2026 | Intraday | `TP1_REWARD_RATIO: 1.5 → 1.75` | Backtest shows 18% improvement |
| May 2026 | Intraday | `MAX_CONCURRENT_TRADES: 3 → 5` | Better capital utilization |
| May 2026 | Intraday | Removed trailing stop — full exit at TP1 | Backtested RR comparison showed no-trail wins on most stocks |
| May 2026 | Intraday | Added `traded_symbols_today` — no re-entry | BHEL entered 3x in one day |
| May 2026 | Intraday | Software SL (LTP monitoring) | STOPMARKET orders rejected by Dhan (DH-906) |
| May 2026 | Intraday | Order fill verification before state add | JKCEMENT orphan short position |
| May 2026 | Intraday | Tick size from instrument_df | DH-16283 order price not multiple of tick |
| May 2026 | Intraday | Slippage tracking on every entry | No visibility on execution quality |
| May 2026 | Intraday | Liquidity impact warning | JKCEMENT low liquidity undetected |
| May 2026 | Intraday | Removed JKCEMENT from watchlist | Tick size issues + low liquidity |
| May 2026 | Intraday | Daily restart cron 8:00 AM IST | State not resetting daily |
| May 2026 | Intraday | Weekly backtest cron Sunday 8 PM | Manual backtest forgotten |
| May 2026 | Intraday | Telegram watchlist report after backtest | No visibility on weekly selection |
| May 2026 | Algo_DOS | Fixed entry_spot as dataclass field | Lost after restart — orphaned field via setattr |
| May 2026 | Algo_DOS | Unified Telegram bot (from credentials) | Two different bots sending alerts |
| May 2026 | Algo_DOS | Dedicated Telegram bot (8987...) | Separate from Intraday for clarity |
| May 2026 | Algo_DOS | Auto-reconnect on empty data | DH-901 silent failure all day |
| May 2026 | Algo_DOS | Daily restart cron 8:05 AM IST | Session invalidated by Intraday restart |
| May 2026 | Algo_DOS | Detailed Telegram entry alert | Only showed symbol + credit before |
| May 2026 | Both | Atomic state writes (tmp + rename) | Crash mid-write corrupts JSON |
| May 2026 | Both | `validate_spread()` called in Algo_DOS | Risk checks existed but were never called |
| 20 May 2026 | Both | Shared Dependencies/ symlink for token | Two services invalidating each other's Dhan session every 60s |
| 20 May 2026 | Intraday | Score threshold 70 → 72 | Borderline scores (69-72) fired due to live vs finalized data noise |
| 20 May 2026 | Intraday | Volume filter: skip if score < 4 | Low-volume signals (RVOL < 0.8x) have no conviction and reverse |
| 20 May 2026 | Intraday | TP1 recalculated from actual fill price | Signal-based TP1 broke RR when slippage occurred (1:1.75 → 1:0.92) |
| 20 May 2026 | Intraday | Robust retry: 3 attempts for entry + exit | Single attempt failures created orphan trades or missed exits |
| 20 May 2026 | Intraday | Order rejection → Telegram alert + abort | Silent failures allowed orphan positions (JKCEMENT naked short) |
| 21 May 2026 | Intraday | Volume score uses full 20-bar history | Session-only baseline inflated by opening rush → unreliable RVOL |
| 21 May 2026 | Intraday | MAX_STOCKS 12 → 15 | More opportunity without quality compromise |
| 21 May 2026 | Intraday | New watchlist from full-history backtest | 48% avg_R improvement: +0.366 → +0.543, win rate 46.6% → 51.9% |
| 21 May 2026 | Intraday | EXCH:16283 fix — integer tick rounding | Float 5512.6000000003 rejected by NSE; integer arithmetic gives clean 5512.60 |
| 21 May 2026 | Intraday | LIMIT buffer 1% → 0.3% | 1% buffer exceeded Dhan intraday price band; JKCEMENT/SKFINDIA/HINDALCO rejected |
| 21 May 2026 | Intraday | Entry retry: 2 LIMIT attempts only | Was LIMIT+LIMIT+MARKET; retrying same broken LIMIT price is pointless |
| 21 May 2026 | Intraday | Reconnect 2-min cooldown | Rapid retries hit Dhan "token once every 2 min" rate limit → cascade failure |
| 21 May 2026 | Intraday | Score breakdown logged at every entry | Trades were unauditable — no way to verify score, RS, VWAP, trend at entry |
| 21 May 2026 | Intraday | Min SL floor 0.3% of entry price | APLAPOLLO SL ₹4.75 (0.26%) hit in 50s — candle wick noise, not real move |
| 21 May 2026 | Intraday | Removed liquidity impact hard block | Per-bar impact % was misleading (0.09% of daily volume = fine); volume score ≥4 is sufficient |

---

## 10. Backtest Lessons — Intraday Strategy

### What Worked
- Score threshold 72 + volume filter ≥4 + full historical baseline: 48% better avg_R
- Full 20-bar historical volume baseline is significantly more accurate than session-only
- Raising threshold filters noise without sacrificing good signals
- Fewer, higher-quality trades outperforms many mediocre ones
- Integer arithmetic for tick rounding eliminates EXCH:16283 rejections

### What to Investigate Next
- Gap-down regime filter (block SHORTs in first 45 min on recovery days)
- Extension filter (skip if stock already moved >1% from open)
- Per-stock direction bias (some stocks work LONG only, some SHORT only)
- Score calculation based on real-time LTP vs closed bar (30-60s lag)
- LIMIT buffer 0.3% — monitor if fills are getting worse before reducing further

---

*This document is a living log. Update it every time a new issue is encountered or a fix is applied.*
