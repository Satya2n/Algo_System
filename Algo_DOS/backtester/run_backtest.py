"""
Algo DOS — Offline Backtest (Local Option Data)
================================================
Uses pre-downloaded ATM-wise option OHLC (monthly expiries, 2021-2025)
from the Stress Testing folder, combined with NIFTY 15-min data
resampled to 60-min, to run a fully offline credit spread backtest.

No API calls required.
"""

import sys
import os
import pandas as pd
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from datetime import timedelta

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.append(PROJECT_ROOT)

RESULTS_DIR = os.path.join(PROJECT_ROOT, "backtester", "results")
os.makedirs(RESULTS_DIR, exist_ok=True)

# =========================================================
# DATA PATHS
# =========================================================
OPTION_DATA_DIR = (
    "/Users/satya/Documents/Dev/Algo_setup/Dhan_lec/Advanced_algo_2026"
    "/12. Stress Testing and Bruteforcing/Dhan Expired Options data"
    "/Options data 60 mins/ATM Wise data/NIFTY"
)
NIFTY_15MIN_PATH = "/Users/satya/Documents/Dev/Algo_setup/Algo_system/core/NIFTY_15_min.csv"

from config.settings import (
    SUPERTREND_ATR, SUPERTREND_MULTIPLIER, BREAKOUT_LOOKBACK,
    MAX_EXTENSION_ATR, MAX_CANDLE_RANGE_ATR, MIN_ATR, TRAIL_TO_BREAKEVEN_PCT,
)
from engine.strategy import StrategyConfig, StrategyEngine

# =========================================================
# SPREAD PARAMETERS
# =========================================================
BULL_SHORT = "ATM-2"   # ~25 delta PUT — sell
BULL_HEDGE = "ATM-5"   # ~10 delta PUT — buy
BEAR_SHORT = "ATM+2"   # ~25 delta CALL — sell
BEAR_HEDGE = "ATM+5"   # ~10 delta CALL — buy

TARGET_PCT = 0.65
STOP_PCT   = 0.85
TRAIL_PCT  = TRAIL_TO_BREAKEVEN_PCT


# =========================================================
# LOT SIZE
# =========================================================
def get_lot_size(trade_date) -> int:
    return 75 if pd.to_datetime(trade_date).date() >= pd.Timestamp("2024-11-28").date() else 50


# =========================================================
# NIFTY UNDERLYING — load + resample to 60-min
# =========================================================
def load_nifty_hourly() -> pd.DataFrame:
    df = pd.read_csv(NIFTY_15MIN_PATH)

    # Normalize timestamp
    ts_col = next((c for c in df.columns if "time" in c.lower()), "timestamp")
    df["timestamp"] = pd.to_datetime(df[ts_col], errors="coerce", utc=True)
    df["timestamp"] = df["timestamp"].dt.tz_convert("Asia/Kolkata").dt.tz_localize(None)
    df = df.dropna(subset=["timestamp"]).sort_values("timestamp")

    df = df.set_index("timestamp")
    for c in ["open", "high", "low", "close", "volume"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")

    # Resample 15-min → 60-min
    hourly = df.resample("60min").agg(
        open=("open", "first"),
        high=("high", "max"),
        low=("low", "min"),
        close=("close", "last"),
        volume=("volume", "sum"),
    ).dropna(subset=["open", "close"]).reset_index()

    # Keep only market hours 09:15–15:15
    t = hourly["timestamp"].dt.time
    hourly = hourly[
        (t >= pd.Timestamp("09:15").time()) &
        (t <= pd.Timestamp("15:15").time())
    ].reset_index(drop=True)

    return hourly


# =========================================================
# EXPIRY RESOLUTION
# =========================================================
def build_expiry_list() -> list[str]:
    """Sorted list of monthly expiry dates available in local data."""
    return sorted(os.listdir(OPTION_DATA_DIR))


def find_expiry(signal_date, expiry_list: list[str]) -> str | None:
    """
    Return the first monthly expiry that is >= signal_date.
    The option file for that expiry starts from the day after the
    previous expiry, so it always covers the signal date.
    """
    sd = pd.to_datetime(signal_date).date()
    for exp in expiry_list:
        if pd.to_datetime(exp).date() >= sd:
            return exp
    return None


# =========================================================
# OPTION DATA LOADER
# =========================================================
def load_option_leg(expiry: str, atm_offset: str, option_type: str) -> pd.DataFrame:
    """Load local ATM-wise option OHLC for one leg."""
    path = os.path.join(
        OPTION_DATA_DIR, expiry, atm_offset,
        f"NIFTY_{expiry}_{option_type}.csv",
    )
    if not os.path.exists(path):
        return pd.DataFrame()

    df = pd.read_csv(path, parse_dates=["datetime"])
    df = df.rename(columns={"datetime": "timestamp"})
    for c in ["open", "high", "low", "close"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df.dropna(subset=["open", "close"]).sort_values("timestamp").reset_index(drop=True)


def slice_leg(df: pd.DataFrame, entry_time, expiry: str) -> pd.DataFrame:
    """Filter option data to the trade holding window."""
    if df.empty:
        return df
    entry_ts  = pd.to_datetime(entry_time)
    expiry_ts = pd.to_datetime(expiry) + pd.Timedelta(hours=16)
    mask = (df["timestamp"] >= entry_ts) & (df["timestamp"] <= expiry_ts)
    return df[mask].reset_index(drop=True)


# =========================================================
# SPREAD SIMULATION
# =========================================================
def simulate_spread(
    short_df: pd.DataFrame,
    hedge_df: pd.DataFrame,
    entry_time,
    signal_side: str,
    trend_lookup: dict,
    lot_size: int,
) -> dict:
    """
    Simulate one credit spread trade using real 60-min option OHLC.

    Exit triggers (checked in order each bar):
      1. Gap open breach
      2. Intra-bar worst-case (short_high - hedge_low) or best-case
      3. Close: trail update, trend reversal, expiry auto-square
    """
    if short_df.empty or hedge_df.empty:
        return {}

    merged = pd.merge(
        short_df[["timestamp", "open", "high", "low", "close"]].rename(
            columns={"open": "s_open", "high": "s_high", "low": "s_low", "close": "s_close"}
        ),
        hedge_df[["timestamp", "open", "high", "low", "close"]].rename(
            columns={"open": "h_open", "high": "h_high", "low": "h_low", "close": "h_close"}
        ),
        on="timestamp",
    ).sort_values("timestamp").reset_index(drop=True)

    entry_ts = pd.to_datetime(entry_time)
    merged   = merged[merged["timestamp"] >= entry_ts].reset_index(drop=True)

    if merged.empty:
        return {}

    entry_credit = float(merged.iloc[0]["s_open"]) - float(merged.iloc[0]["h_open"])
    if entry_credit <= 0.01:
        return {}

    target_value = entry_credit * (1.0 - TARGET_PCT)
    stop_value   = entry_credit * (1.0 + STOP_PCT)
    sl_moved     = False

    def result(reason, ts, exit_credit):
        return {
            "exit_reason":  reason,
            "exit_time":    ts,
            "entry_credit": round(entry_credit, 2),
            "exit_credit":  round(float(exit_credit), 2),
            "pnl":          round((entry_credit - float(exit_credit)) * lot_size, 2),
        }

    for _, row in merged.iterrows():
        ts          = row["timestamp"]
        active_stop = entry_credit if sl_moved else stop_value

        # 1. Gap open
        open_credit = float(row["s_open"]) - float(row["h_open"])
        if open_credit >= active_stop:
            return result("Gap Stop Loss", ts, open_credit)
        if open_credit <= target_value:
            return result("Gap Target Hit", ts, open_credit)

        # 2. Intra-bar extremes
        worst = float(row["s_high"]) - float(row["h_low"])
        best  = float(row["s_low"])  - float(row["h_high"])
        if worst >= active_stop:
            return result("Intra-Bar Stop Loss", ts, active_stop)
        if best <= target_value:
            return result("Intra-Bar Target Hit", ts, target_value)

        # 3. Close-of-bar
        close_credit = float(row["s_close"]) - float(row["h_close"])

        # Trail to breakeven
        if not sl_moved and close_credit <= entry_credit * (1.0 - TRAIL_PCT):
            sl_moved = True

        # Trend reversal
        hour_key = ts.replace(minute=0, second=0, microsecond=0)
        trend    = trend_lookup.get(hour_key, "")
        if signal_side == "BULLISH_ENTRY" and trend == "bearish":
            return result("Trend Reversal", ts, close_credit)
        if signal_side == "BEARISH_ENTRY" and trend == "bullish":
            return result("Trend Reversal", ts, close_credit)

        # Monthly expiry (last Thursday of month, last bar ~15:15)
        if ts.weekday() == 3 and ts.hour >= 15:
            exp_day = pd.to_datetime(ts).date()
            # Check it's actually the last Thursday (next Thursday is in next month)
            next_thu = exp_day + timedelta(days=7)
            if next_thu.month != exp_day.month:
                return result("Expiry Auto-Square", ts, close_credit)

    last = merged.iloc[-1]
    exit_credit = float(last["s_close"]) - float(last["h_close"])
    return result("End of Data", last["timestamp"], exit_credit)


# =========================================================
# METRICS + PLOT
# =========================================================
def print_metrics(df: pd.DataFrame):
    wins   = df[df["pnl"] > 0]
    losses = df[df["pnl"] <= 0]
    gp = wins["pnl"].sum()
    gl = losses["pnl"].sum()
    equity   = df["pnl"].cumsum()
    drawdown = equity - equity.cummax()

    print("\n" + "=" * 45)
    print("         BACKTEST RESULTS (2021-2025)")
    print("=" * 45)
    print(f"Total Trades   : {len(df)}")
    print(f"Win Rate       : {len(wins)/len(df)*100:.1f}%")
    print(f"Total PnL      : ₹{df['pnl'].sum():>12,.2f}")
    print(f"Avg Win        : ₹{wins['pnl'].mean():>12,.2f}" if not wins.empty else "Avg Win        : —")
    print(f"Avg Loss       : ₹{losses['pnl'].mean():>12,.2f}" if not losses.empty else "Avg Loss       : —")
    pf = gp / abs(gl) if gl != 0 else float("inf")
    print(f"Profit Factor  : {pf:.2f}")
    print(f"Max Drawdown   : ₹{drawdown.min():>12,.2f}")
    print(f"\nExit Reason Breakdown:")
    for reason, count in df["exit_reason"].value_counts().items():
        print(f"  {reason:<30} {count}")
    print("=" * 45)


def plot_equity(df: pd.DataFrame):
    df = df.copy()
    df["cumulative_pnl"] = df["pnl"].cumsum()

    plt.figure(figsize=(14, 5))
    plt.plot(pd.to_datetime(df["entry_date"]), df["cumulative_pnl"], marker="o", ms=4, linewidth=1.5)
    plt.fill_between(pd.to_datetime(df["entry_date"]), df["cumulative_pnl"], alpha=0.1)
    plt.title("Algo DOS — Equity Curve (Real Option Data, 2021–2025)")
    plt.xlabel("Entry Date")
    plt.ylabel("Cumulative PnL (₹)")
    plt.axhline(0, color="red", linestyle="--", linewidth=0.8)
    plt.grid(True, alpha=0.3)
    plt.tight_layout()
    path = os.path.join(RESULTS_DIR, "equity_curve.png")
    plt.savefig(path, dpi=150)
    plt.close()
    print(f"Equity curve saved → {path}")


# =========================================================
# MAIN
# =========================================================
def run():
    print("=" * 50)
    print("  ALGO DOS — OFFLINE BACKTEST (2021–2025)")
    print("=" * 50)

    # 1. Load NIFTY 60-min data
    print("\nLoading NIFTY 15-min data → resampling to 60-min...")
    nifty_df = load_nifty_hourly()
    print(f"NIFTY 60-min rows: {len(nifty_df)} | "
          f"{nifty_df['timestamp'].iloc[0].date()} → {nifty_df['timestamp'].iloc[-1].date()}")

    # 2. Run strategy pipeline
    print("\nRunning strategy pipeline (Supertrend + Breakout)...")
    strategy = StrategyEngine(StrategyConfig(
        atr_period=SUPERTREND_ATR,
        atr_multiplier=SUPERTREND_MULTIPLIER,
        breakout_lookback=BREAKOUT_LOOKBACK,
        max_extension_atr=MAX_EXTENSION_ATR,
        max_candle_range_atr=MAX_CANDLE_RANGE_ATR,
        min_atr=MIN_ATR,
    ))
    signal_df = strategy.run_pipeline(nifty_df)

    # Build hourly trend lookup
    trend_lookup = {
        row["timestamp"]: row.get("trend", "")
        for _, row in signal_df.iterrows()
        if pd.notna(row["timestamp"])
    }

    # 3. Filter to signal rows within option data coverage
    expiry_list  = build_expiry_list()
    data_end     = pd.to_datetime(expiry_list[-1])

    entry_rows = signal_df[
        (signal_df["signal"] != "NO_TRADE") &
        (signal_df["timestamp"] <= data_end)
    ].copy()

    # 1 trade per day max
    entry_rows["date"] = entry_rows["timestamp"].dt.date
    entry_rows = entry_rows.drop_duplicates(subset="date", keep="first")

    print(f"Signals in option data range: {len(entry_rows)}")

    # 4. Simulate each trade
    results = []
    skipped = 0

    for i, (_, row) in enumerate(entry_rows.iterrows(), 1):
        signal     = row["signal"]
        entry_time = row["timestamp"]
        entry_date = str(entry_time.date())
        expiry     = find_expiry(entry_date, expiry_list)
        lot_size   = get_lot_size(entry_date)

        if expiry is None:
            skipped += 1
            continue

        if signal == "BULLISH_ENTRY":
            option_type  = "PUT"
            short_offset = BULL_SHORT
            hedge_offset = BULL_HEDGE
        else:
            option_type  = "CALL"
            short_offset = BEAR_SHORT
            hedge_offset = BEAR_HEDGE

        print(f"[{i:>3}/{len(entry_rows)}] {signal:<16} | {entry_date} → expiry {expiry} "
              f"| {short_offset}/{hedge_offset} {option_type} | lot={lot_size}")

        short_full = load_option_leg(expiry, short_offset, option_type)
        hedge_full = load_option_leg(expiry, hedge_offset, option_type)

        short_df = slice_leg(short_full, entry_time, expiry)
        hedge_df = slice_leg(hedge_full, entry_time, expiry)

        if short_df.empty or hedge_df.empty:
            print(f"           → Skipped: no option data for this window")
            skipped += 1
            continue

        res = simulate_spread(short_df, hedge_df, entry_time, signal, trend_lookup, lot_size)

        if not res:
            print(f"           → Skipped: zero/negative entry credit")
            skipped += 1
            continue

        res.update({
            "entry_time":    str(entry_time),
            "entry_date":    entry_date,
            "expiry":        expiry,
            "signal":        signal,
            "short_offset":  short_offset,
            "hedge_offset":  hedge_offset,
            "option_type":   option_type,
            "lot_size":      lot_size,
        })
        results.append(res)
        print(f"           → {res['exit_reason']:<25} | credit {res['entry_credit']:.1f}→{res['exit_credit']:.1f} | PnL ₹{res['pnl']:>8,.0f}")

    print(f"\n{len(results)} trades simulated | {skipped} skipped")

    if not results:
        print("No trades to analyse.")
        return

    # 5. Save + report
    trade_df = pd.DataFrame(results)
    csv_path = os.path.join(RESULTS_DIR, "backtest_trades.csv")
    trade_df.to_csv(csv_path, index=False)
    print(f"Trade log → {csv_path}")

    print_metrics(trade_df)
    plot_equity(trade_df)


if __name__ == "__main__":
    run()
