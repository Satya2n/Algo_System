"""
Weekly Watchlist Generator
===========================
Run this every week before the market opens to refresh your stock watchlist.

What it does:
  1. Fetches last 3 months of 5-min data for every NIFTY 200 stock
  2. Runs the same pullback + RS-scoring strategy used in live trading
  3. Filters stocks that have a proven edge (profit factor, avg R, win rate)
  4. Saves the shortlist to data/preferred_watchlist.csv

Usage:
    cd Intraday_strategy_stock_buying
    python3 backtest/run_backtest.py

Output:
    data/preferred_watchlist.csv   — feed directly into the live engine
    backtest/results/report.csv    — full per-stock metrics
"""

import sys
import os
import time
import logging
from datetime import datetime
from pathlib import Path

import pandas as pd
import numpy as np

# ── path setup ────────────────────────────────────────────────────────────────
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from config.credentials import (
    DHAN_CLIENT_CODE, DHAN_ACCESS_TOKEN, DHAN_PIN, DHAN_TOTP_SECRET,
)
from engine.strategy import (
    standardize_df, add_full_indicators, get_today_session,
    add_session_indicators, rank_stock, prepare_live_df,
    confirm_long_pullback, confirm_short_pullback,
)
from backtest.universe import NIFTY_200
from Dhan_Tradehull import Tradehull

# ── output dirs ───────────────────────────────────────────────────────────────
RESULTS_DIR  = ROOT / "backtest" / "results"
WATCHLIST_OUT = ROOT / "data" / "preferred_watchlist.csv"
RESULTS_DIR.mkdir(parents=True, exist_ok=True)

# ── logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[
        logging.FileHandler(RESULTS_DIR / "backtest.log"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("BACKTEST")

# ── filter thresholds (adjust to tighten/loosen the shortlist) ────────────────
MIN_TRADES        = 8      # ignore stocks with fewer signals (too thin)
MIN_PROFIT_FACTOR = 1.2    # at least 20% more wins than losses by value
MIN_AVG_R         = 0.05   # average R-multiple must be positive & meaningful
MAX_STOCKS        = 15     # cap watchlist so live engine doesn't spread too thin

# ── simulation settings ───────────────────────────────────────────────────────
TP1_RR           = 1.5    # reward:risk ratio for first partial exit
ENTRY_START_H    = (9, 30)
ENTRY_END_H      = (12, 0) # no new entries after noon
FORCE_EXIT_H     = (15, 15)
BROKERAGE        = 40.0   # ₹ per trade (round-trip, both legs)
SLEEP_BETWEEN    = 1.2    # seconds between API calls (avoid rate limits)


# ══════════════════════════════════════════════════════════════════════════════
# BROKER CONNECTION
# ══════════════════════════════════════════════════════════════════════════════

def connect() -> Tradehull:
    if DHAN_ACCESS_TOKEN:
        logger.info("Login: access token")
        return Tradehull(DHAN_CLIENT_CODE, DHAN_ACCESS_TOKEN, mode="access_token")
    if DHAN_PIN and DHAN_TOTP_SECRET:
        logger.info("Login: PIN + TOTP")
        return Tradehull(DHAN_CLIENT_CODE, mode="pin_totp",
                         pin=DHAN_PIN, totp_secret=DHAN_TOTP_SECRET)
    raise ValueError("No credentials. Set DHAN_ACCESS_TOKEN or DHAN_PIN + DHAN_TOTP_SECRET.")


# ══════════════════════════════════════════════════════════════════════════════
# TIME HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def _to_naive(ts):
    """Strip timezone from a Timestamp."""
    if hasattr(ts, "tz_convert") and ts.tz is not None:
        ts = ts.tz_convert("Asia/Kolkata")
    if hasattr(ts, "tz_localize"):
        ts = ts.tz_localize(None)
    return ts


def is_entry_bar(ts) -> bool:
    """True if the bar falls in the allowed entry window 09:30–12:00."""
    ts = _to_naive(ts)
    t  = ts.time()
    from datetime import time as dtime
    return dtime(*ENTRY_START_H) <= t < dtime(*ENTRY_END_H)


def is_force_exit_bar(ts) -> bool:
    """True if the bar is at or past the force-exit time 15:15."""
    ts = _to_naive(ts)
    from datetime import time as dtime
    return ts.time() >= dtime(*FORCE_EXIT_H)


# ══════════════════════════════════════════════════════════════════════════════
# TRADE PLAN
# ══════════════════════════════════════════════════════════════════════════════

def build_trade_plan(live_df: pd.DataFrame, signal: str):
    """Return entry/sl/tp1/side dict or None if risk is zero."""
    latest = live_df.iloc[-1]
    buf    = 0.001  # 0.1% buffer on SL

    if signal == "LONG":
        entry = float(latest["close"])
        sl    = float(latest["low"]) * (1 - buf)
        risk  = entry - sl
        if risk <= 0:
            return None
        return {"side": "BUY",  "entry": entry, "sl": sl, "tp1": entry + TP1_RR * risk, "risk": risk}

    elif signal == "SHORT":
        entry = float(latest["close"])
        sl    = float(latest["high"]) * (1 + buf)
        risk  = sl - entry
        if risk <= 0:
            return None
        return {"side": "SELL", "entry": entry, "sl": sl, "tp1": entry - TP1_RR * risk, "risk": risk}

    return None


# ══════════════════════════════════════════════════════════════════════════════
# TRADE SIMULATION
# ══════════════════════════════════════════════════════════════════════════════

def simulate_trade(stock_df: pd.DataFrame, entry_idx: int, plan: dict):
    """
    Simulate:
      - Immediate SL hit → full loss
      - TP1 hit first → book 50%, move SL to breakeven, trail rest with 3-bar low/high
      - Force exit at 15:15 on same day

    Returns (r_multiple, outcome_label)
    """
    side   = plan["side"]
    entry  = plan["entry"]
    sl     = plan["sl"]
    tp1    = plan["tp1"]
    risk   = plan["risk"]

    future = stock_df.iloc[entry_idx + 1:].copy()
    if future.empty:
        return 0.0, "NO_DATA"

    partial_done  = False
    trail_sl      = sl
    half = 0.5
    rem  = 0.5

    partial_pnl = 0.0
    second_exit = None

    # Determine same-day boundary
    entry_date = _to_naive(stock_df.index[entry_idx]).date()

    for j in range(len(future)):
        bar      = future.iloc[j]
        bar_time = future.index[j]
        bar_ts   = _to_naive(bar_time)

        # Force exit at end of day
        if bar_ts.date() != entry_date or is_force_exit_bar(bar_ts):
            close = float(bar["close"])
            if partial_done:
                second_exit = close
            else:
                pnl_pts = (close - entry) if side == "BUY" else (entry - close)
                return pnl_pts / risk, "FORCE_EXIT"
            break

        hi = float(bar["high"])
        lo = float(bar["low"])

        if not partial_done:
            # SL check
            if (side == "BUY"  and lo  <= sl) or \
               (side == "SELL" and hi  >= sl):
                return -1.0, "SL_HIT"

            # TP1 check
            if (side == "BUY"  and hi  >= tp1) or \
               (side == "SELL" and lo  <= tp1):
                partial_done = True
                partial_pnl  = TP1_RR * half   # 50% at TP1 = 0.75R
                trail_sl     = entry            # move SL to breakeven

        else:
            # Trail with last 3 bars
            if j >= 2:
                last3 = future.iloc[max(0, j - 2): j + 1]
                if side == "BUY":
                    new_trail = float(last3["low"].min())
                    trail_sl  = max(trail_sl, new_trail)
                    if lo <= trail_sl:
                        second_exit = trail_sl
                        break
                else:
                    new_trail = float(last3["high"].max())
                    trail_sl  = min(trail_sl, new_trail)
                    if hi >= trail_sl:
                        second_exit = trail_sl
                        break

    # If partial and no trail exit yet, use last close
    if partial_done and second_exit is None:
        last = future.iloc[-1]
        second_exit = float(last["close"])

    if not partial_done:
        # Timed out without TP1
        last    = future.iloc[-1]
        pnl_pts = (float(last["close"]) - entry) if side == "BUY" else (entry - float(last["close"]))
        return pnl_pts / risk, "TIMEOUT"

    second_r = ((second_exit - entry) / risk) if side == "BUY" else \
               ((entry - second_exit) / risk)

    total_r = partial_pnl + second_r * rem
    return total_r, "TP1_TRAIL"


# ══════════════════════════════════════════════════════════════════════════════
# PER-SYMBOL BACKTEST
# ══════════════════════════════════════════════════════════════════════════════

def backtest_symbol(symbol: str, stock_df, nifty_df) -> tuple[list, dict]:
    """
    Returns (trade_rows, metrics_dict).
    trade_rows: list of dicts, one per trade
    metrics_dict: summary stats for this symbol
    """
    try:
        stock = standardize_df(stock_df).copy()
        nifty = standardize_df(nifty_df).copy()

        stock = add_full_indicators(stock).dropna().copy()
        nifty = add_full_indicators(nifty).dropna().copy()
    except Exception as e:
        return [], {"error": str(e)}

    if stock.empty or nifty.empty or len(stock) < 60:
        return [], {"error": "insufficient data"}

    trades = []
    max_i  = min(len(stock), len(nifty)) - 5

    for i in range(50, max_i):
        bar_ts = stock.index[i]
        if not is_entry_bar(bar_ts):
            continue

        stock_slice = stock.iloc[:i + 1]
        nifty_slice = nifty.iloc[:i + 1]

        rank = rank_stock(stock_slice, nifty_slice)
        if not rank or rank["decision"] == "SKIP":
            continue

        live_df = prepare_live_df(stock_slice)
        if live_df is None or len(live_df) < 4:
            continue

        decision = rank["decision"]
        signal   = None

        if decision == "LONG":
            ok, _ = confirm_long_pullback(live_df)
            if ok:
                signal = "LONG"
        elif decision == "SHORT":
            ok, _ = confirm_short_pullback(live_df)
            if ok:
                signal = "SHORT"

        if not signal:
            continue

        plan = build_trade_plan(live_df, signal)
        if not plan:
            continue

        r_mult, outcome = simulate_trade(stock, i, plan)

        trades.append({
            "symbol":  symbol,
            "signal":  signal,
            "entry":   round(plan["entry"], 2),
            "sl":      round(plan["sl"], 2),
            "tp1":     round(plan["tp1"], 2),
            "r_mult":  round(r_mult, 3),
            "outcome": outcome,
        })

    if not trades:
        return [], {"trades": 0, "win_rate": 0, "avg_r": 0, "profit_factor": 0}

    df   = pd.DataFrame(trades)
    wins = df[df["r_mult"] > 0]
    loss = df[df["r_mult"] <= 0]
    gp   = wins["r_mult"].sum()
    gl   = abs(loss["r_mult"].sum())
    pf   = round(gp / gl, 2) if gl > 0 else float("inf")

    metrics = {
        "trades":        len(df),
        "win_rate":      round(len(wins) / len(df) * 100, 1),
        "avg_r":         round(df["r_mult"].mean(), 3),
        "profit_factor": pf,
        "long_trades":   int((df["signal"] == "LONG").sum()),
        "short_trades":  int((df["signal"] == "SHORT").sum()),
    }
    return trades, metrics


# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════

def run():
    start_wall = time.time()
    logger.info("=" * 60)
    logger.info(f"  WEEKLY BACKTEST — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    logger.info(f"  Universe: {len(NIFTY_200)} stocks | Timeframe: 5-min (last 3 months)")
    logger.info("=" * 60)

    # ── connect ───────────────────────────────────────────────────────────────
    tsl = connect()

    # ── fetch NIFTY ───────────────────────────────────────────────────────────
    logger.info("Fetching NIFTY index data...")
    nifty_df = tsl.get_historical_data(
        tradingsymbol="NIFTY", exchange="INDEX", timeframe="5"
    )
    if nifty_df is None or nifty_df.empty:
        logger.error("Could not fetch NIFTY data. Aborting.")
        return

    logger.info(f"NIFTY: {len(nifty_df)} bars fetched.")

    # ── backtest each symbol ──────────────────────────────────────────────────
    all_trades   = []
    symbol_stats = []
    skipped      = []

    total = len(NIFTY_200)
    for idx, sym in enumerate(NIFTY_200, 1):
        logger.info(f"[{idx:>3}/{total}] {sym:<20}", )

        try:
            time.sleep(SLEEP_BETWEEN)
            stock_df = tsl.get_historical_data(
                tradingsymbol=sym, exchange="NSE", timeframe="5"
            )

            if stock_df is None or stock_df.empty or len(stock_df) < 100:
                logger.info(f"        → skipped (no data)")
                skipped.append(sym)
                continue

            trades, metrics = backtest_symbol(sym, stock_df, nifty_df)

            if "error" in metrics:
                logger.info(f"        → skipped ({metrics['error']})")
                skipped.append(sym)
                continue

            metrics["symbol"] = sym
            symbol_stats.append(metrics)
            all_trades.extend(trades)

            logger.info(
                f"        → {metrics['trades']:>3} trades | "
                f"WR {metrics['win_rate']:>5.1f}% | "
                f"avg_R {metrics['avg_r']:>+.3f} | "
                f"PF {metrics['profit_factor']:>4.2f}"
            )

        except Exception as e:
            logger.warning(f"        → error: {e}")
            skipped.append(sym)

    # ── build report ──────────────────────────────────────────────────────────
    if not symbol_stats:
        logger.error("No symbols backtested successfully.")
        return

    stats_df = pd.DataFrame(symbol_stats).sort_values("avg_r", ascending=False)

    # ── filter preferred ──────────────────────────────────────────────────────
    preferred = stats_df[
        (stats_df["trades"]        >= MIN_TRADES)       &
        (stats_df["profit_factor"] >= MIN_PROFIT_FACTOR) &
        (stats_df["avg_r"]         >= MIN_AVG_R)
    ].head(MAX_STOCKS)

    # ── save results ──────────────────────────────────────────────────────────
    report_path = RESULTS_DIR / f"report_{datetime.now().strftime('%Y%m%d')}.csv"
    stats_df.to_csv(report_path, index=False)
    logger.info(f"\nFull report saved → {report_path}")

    if not preferred.empty:
        preferred[["symbol"]].to_csv(WATCHLIST_OUT, index=False)
        logger.info(f"Watchlist saved → {WATCHLIST_OUT}")
    else:
        logger.warning("No stocks passed the filter. Watchlist NOT updated.")

    # ── print summary ─────────────────────────────────────────────────────────
    elapsed = (time.time() - start_wall) / 60
    print("\n" + "=" * 60)
    print(f"  BACKTEST COMPLETE  ({elapsed:.1f} min)")
    print("=" * 60)
    print(f"  Universe   : {total} stocks")
    print(f"  Backtested : {len(symbol_stats)}")
    print(f"  Skipped    : {len(skipped)}")
    print(f"  Qualified  : {len(preferred)}")
    print()

    if not preferred.empty:
        print(f"{'SYMBOL':<16} {'TRADES':>6} {'WIN%':>6} {'AVG_R':>7} {'PF':>6}")
        print("-" * 46)
        for _, row in preferred.iterrows():
            print(
                f"{row['symbol']:<16} "
                f"{int(row['trades']):>6} "
                f"{row['win_rate']:>5.1f}% "
                f"{row['avg_r']:>+7.3f} "
                f"{row['profit_factor']:>6.2f}"
            )
        print()
        print("✅ Watchlist updated:")
        print("  ", ", ".join(preferred["symbol"].tolist()))
    else:
        print("⚠️  No stocks passed the filter this week.")
        print("   Tip: loosen MIN_TRADES / MIN_PROFIT_FACTOR / MIN_AVG_R thresholds.")

    print("=" * 60)

    # ── signal breakdown ──────────────────────────────────────────────────────
    if all_trades:
        all_df  = pd.DataFrame(all_trades)
        long_r  = all_df[all_df["signal"] == "LONG"]["r_mult"].mean()
        short_r = all_df[all_df["signal"] == "SHORT"]["r_mult"].mean()
        print(f"\nSignal edge across all qualified stocks:")
        print(f"  LONG  trades avg R = {long_r:+.3f}")
        print(f"  SHORT trades avg R = {short_r:+.3f}")


if __name__ == "__main__":
    run()
