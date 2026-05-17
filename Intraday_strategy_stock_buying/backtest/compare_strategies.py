"""
Strategy Comparison: With Trailing vs Without Trailing
=======================================================
Fetches data once per stock and runs two simulations:

  Strategy A — With Trailing  (current live approach)
    SL hit             → exit 100%, -1R
    TP1 hit (1.5R)     → exit 50%, move SL to breakeven, trail rest with 3-bar extremes

  Strategy B — No Trailing  (simple fixed exit)
    SL hit             → exit 100%, -1R
    TP1 hit (1.5R)     → exit 100% immediately, take full +1.5R

Usage:
    cd Intraday_strategy_stock_buying
    python3 backtest/compare_strategies.py

Output:
    backtest/results/comparison_YYYYMMDD.csv  — full per-stock breakdown
    Prints a side-by-side summary table
"""

import sys
import os
import time
import logging
from datetime import datetime
from pathlib import Path

import pandas as pd
import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from config.credentials import (
    DHAN_CLIENT_CODE, DHAN_ACCESS_TOKEN, DHAN_PIN, DHAN_TOTP_SECRET,
)
from engine.strategy import (
    standardize_df, add_full_indicators,
    rank_stock, prepare_live_df,
    confirm_long_pullback, confirm_short_pullback,
)
from backtest.universe import NIFTY_200
from Dhan_Tradehull import Tradehull

RESULTS_DIR = ROOT / "backtest" / "results"
RESULTS_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[
        logging.FileHandler(RESULTS_DIR / "compare.log"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("COMPARE")

# ── settings ──────────────────────────────────────────────────────────────────
TP1_RR         = 1.5
ENTRY_START    = (9, 30)
ENTRY_END      = (12, 0)
FORCE_EXIT_T   = (15, 15)
SLEEP_BETWEEN  = 1.2


# ── helpers ───────────────────────────────────────────────────────────────────

def connect() -> Tradehull:
    if DHAN_ACCESS_TOKEN:
        return Tradehull(DHAN_CLIENT_CODE, DHAN_ACCESS_TOKEN, mode="access_token")
    if DHAN_PIN and DHAN_TOTP_SECRET:
        return Tradehull(DHAN_CLIENT_CODE, mode="pin_totp",
                         pin=DHAN_PIN, totp_secret=DHAN_TOTP_SECRET)
    raise ValueError("No credentials configured.")


def _naive(ts):
    if hasattr(ts, "tz_convert") and ts.tz is not None:
        ts = ts.tz_convert("Asia/Kolkata").tz_localize(None)
    return ts


def is_entry_bar(ts) -> bool:
    from datetime import time as dtime
    t = _naive(ts).time()
    return dtime(*ENTRY_START) <= t < dtime(*ENTRY_END)


def is_force_exit(ts) -> bool:
    from datetime import time as dtime
    return _naive(ts).time() >= dtime(*FORCE_EXIT_T)


def build_plan(live_df, signal):
    latest = live_df.iloc[-1]
    buf    = 0.001
    if signal == "LONG":
        entry = float(latest["close"])
        sl    = float(latest["low"]) * (1 - buf)
        risk  = entry - sl
        if risk <= 0:
            return None
        return {"side": "BUY",  "entry": entry, "sl": sl,
                "tp1": entry + TP1_RR * risk, "risk": risk}
    elif signal == "SHORT":
        entry = float(latest["close"])
        sl    = float(latest["high"]) * (1 + buf)
        risk  = sl - entry
        if risk <= 0:
            return None
        return {"side": "SELL", "entry": entry, "sl": sl,
                "tp1": entry - TP1_RR * risk, "risk": risk}
    return None


# ══════════════════════════════════════════════════════════════════════════════
# SIMULATION A — WITH TRAILING (current live strategy)
# ══════════════════════════════════════════════════════════════════════════════

def sim_with_trail(stock_df, entry_idx, plan) -> tuple[float, str]:
    """50% at TP1, trail remaining 50% with 3-bar extremes."""
    side  = plan["side"]
    entry = plan["entry"]
    sl    = plan["sl"]
    tp1   = plan["tp1"]
    risk  = plan["risk"]

    future     = stock_df.iloc[entry_idx + 1:]
    if future.empty:
        return 0.0, "NO_DATA"

    partial    = False
    trail_sl   = sl
    second_exit = None
    entry_date = _naive(stock_df.index[entry_idx]).date()

    for j in range(len(future)):
        bar  = future.iloc[j]
        ts   = _naive(future.index[j])
        hi   = float(bar["high"])
        lo   = float(bar["low"])

        # Force exit
        if ts.date() != entry_date or is_force_exit(ts):
            close = float(bar["close"])
            if partial:
                second_exit = close
            else:
                pts = (close - entry) if side == "BUY" else (entry - close)
                return pts / risk, "FORCE_EXIT"
            break

        if not partial:
            if (side == "BUY"  and lo <= sl) or (side == "SELL" and hi >= sl):
                return -1.0, "SL_HIT"
            if (side == "BUY"  and hi >= tp1) or (side == "SELL" and lo <= tp1):
                partial   = True
                trail_sl  = entry     # move to breakeven
        else:
            if j >= 2:
                last3 = future.iloc[max(0, j - 2): j + 1]
                if side == "BUY":
                    trail_sl = max(trail_sl, float(last3["low"].min()))
                    if lo <= trail_sl:
                        second_exit = trail_sl
                        break
                else:
                    trail_sl = min(trail_sl, float(last3["high"].max()))
                    if hi >= trail_sl:
                        second_exit = trail_sl
                        break

    if partial and second_exit is None:
        second_exit = float(future.iloc[-1]["close"])

    if not partial:
        close = float(future.iloc[-1]["close"])
        pts   = (close - entry) if side == "BUY" else (entry - close)
        return pts / risk, "TIMEOUT"

    half_r   = TP1_RR * 0.5
    second_r = ((second_exit - entry) / risk if side == "BUY"
                else (entry - second_exit) / risk)
    return half_r + second_r * 0.5, "TP1_TRAIL"


# ══════════════════════════════════════════════════════════════════════════════
# SIMULATION B — NO TRAILING (full exit at TP1)
# ══════════════════════════════════════════════════════════════════════════════

def sim_no_trail(stock_df, entry_idx, plan) -> tuple[float, str]:
    """Exit 100% at SL (-1R) or TP1 (+1.5R). No trail, no partial."""
    side  = plan["side"]
    entry = plan["entry"]
    sl    = plan["sl"]
    tp1   = plan["tp1"]
    risk  = plan["risk"]

    future     = stock_df.iloc[entry_idx + 1:]
    if future.empty:
        return 0.0, "NO_DATA"

    entry_date = _naive(stock_df.index[entry_idx]).date()

    for j in range(len(future)):
        bar = future.iloc[j]
        ts  = _naive(future.index[j])
        hi  = float(bar["high"])
        lo  = float(bar["low"])

        # Force exit at end of day
        if ts.date() != entry_date or is_force_exit(ts):
            close = float(bar["close"])
            pts   = (close - entry) if side == "BUY" else (entry - close)
            return pts / risk, "FORCE_EXIT"

        # SL check first (conservative — whichever bar hits first)
        if (side == "BUY"  and lo <= sl) or (side == "SELL" and hi >= sl):
            return -1.0, "SL_HIT"

        # TP1 check
        if (side == "BUY"  and hi >= tp1) or (side == "SELL" and lo <= tp1):
            return TP1_RR, "TP1_FULL"

    # Timed out, exit at last close
    close = float(future.iloc[-1]["close"])
    pts   = (close - entry) if side == "BUY" else (entry - close)
    return pts / risk, "TIMEOUT"


# ══════════════════════════════════════════════════════════════════════════════
# PER-SYMBOL COMPARISON
# ══════════════════════════════════════════════════════════════════════════════

def compare_symbol(symbol, stock_df, nifty_df):
    try:
        stock = add_full_indicators(standardize_df(stock_df)).dropna().copy()
        nifty = add_full_indicators(standardize_df(nifty_df)).dropna().copy()
    except Exception as e:
        return None

    if stock.empty or nifty.empty or len(stock) < 60:
        return None

    results_a = []   # with trail
    results_b = []   # no trail

    max_i = min(len(stock), len(nifty)) - 5

    for i in range(50, max_i):
        if not is_entry_bar(stock.index[i]):
            continue

        rank = rank_stock(stock.iloc[:i + 1], nifty.iloc[:i + 1])
        if not rank or rank["decision"] == "SKIP":
            continue

        live_df = prepare_live_df(stock.iloc[:i + 1])
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

        plan = build_plan(live_df, signal)
        if not plan:
            continue

        r_a, out_a = sim_with_trail(stock, i, plan)
        r_b, out_b = sim_no_trail(stock, i, plan)

        results_a.append(r_a)
        results_b.append(r_b)

    if not results_a:
        return None

    def metrics(rs):
        arr = np.array(rs)
        wins = arr[arr > 0]
        loss = arr[arr <= 0]
        gp   = wins.sum()
        gl   = abs(loss.sum())
        return {
            "trades":        len(arr),
            "win_rate":      round(len(wins) / len(arr) * 100, 1),
            "avg_r":         round(arr.mean(), 3),
            "profit_factor": round(gp / gl, 2) if gl > 0 else float("inf"),
            "total_r":       round(arr.sum(), 2),
        }

    ma = metrics(results_a)
    mb = metrics(results_b)

    return {
        "symbol":          symbol,
        "trades":          ma["trades"],
        # Strategy A — with trail
        "a_win_rate":      ma["win_rate"],
        "a_avg_r":         ma["avg_r"],
        "a_profit_factor": ma["profit_factor"],
        "a_total_r":       ma["total_r"],
        # Strategy B — no trail
        "b_win_rate":      mb["win_rate"],
        "b_avg_r":         mb["avg_r"],
        "b_profit_factor": mb["profit_factor"],
        "b_total_r":       mb["total_r"],
        # winner
        "better":          "TRAIL" if ma["avg_r"] > mb["avg_r"] else "NO_TRAIL",
        "edge_diff":       round(ma["avg_r"] - mb["avg_r"], 3),
    }


# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════

def run():
    t0 = time.time()
    logger.info("=" * 65)
    logger.info(f"  STRATEGY COMPARISON — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    logger.info(f"  A: With Trailing  |  B: No Trailing (full exit at TP1)")
    logger.info("=" * 65)

    tsl = connect()

    logger.info("Fetching NIFTY data...")
    nifty_df = tsl.get_historical_data(tradingsymbol="NIFTY", exchange="INDEX", timeframe="5")
    if nifty_df is None or nifty_df.empty:
        logger.error("NIFTY fetch failed. Aborting.")
        return

    logger.info(f"NIFTY: {len(nifty_df)} bars")

    rows    = []
    skipped = []
    total   = len(NIFTY_200)

    for idx, sym in enumerate(NIFTY_200, 1):
        logger.info(f"[{idx:>3}/{total}] {sym:<20}", )
        try:
            time.sleep(SLEEP_BETWEEN)
            stock_df = tsl.get_historical_data(tradingsymbol=sym, exchange="NSE", timeframe="5")

            if stock_df is None or stock_df.empty or len(stock_df) < 100:
                logger.info("         → skipped (no data)")
                skipped.append(sym)
                continue

            row = compare_symbol(sym, stock_df, nifty_df)
            if row is None:
                logger.info("         → skipped (insufficient signals)")
                skipped.append(sym)
                continue

            rows.append(row)
            logger.info(
                f"         A(trail):   avg_R {row['a_avg_r']:>+.3f}  PF {row['a_profit_factor']:>4.2f} | "
                f"B(no trail): avg_R {row['b_avg_r']:>+.3f}  PF {row['b_profit_factor']:>4.2f} | "
                f"→ {row['better']}"
            )

        except Exception as e:
            logger.warning(f"         → error: {e}")
            skipped.append(sym)

    if not rows:
        logger.error("No results collected.")
        return

    df = pd.DataFrame(rows)

    # ── save ─────────────────────────────────────────────────────────────────
    out_path = RESULTS_DIR / f"comparison_{datetime.now().strftime('%Y%m%d')}.csv"
    df.to_csv(out_path, index=False)
    logger.info(f"\nFull results saved → {out_path}")

    # ── overall winner ────────────────────────────────────────────────────────
    trail_wins   = (df["better"] == "TRAIL").sum()
    notail_wins  = (df["better"] == "NO_TRAIL").sum()

    overall_a_r  = df["a_avg_r"].mean()
    overall_b_r  = df["b_avg_r"].mean()
    overall_a_pf = df["a_profit_factor"].replace(float("inf"), np.nan).mean()
    overall_b_pf = df["b_profit_factor"].replace(float("inf"), np.nan).mean()

    elapsed = (time.time() - t0) / 60

    print("\n" + "=" * 65)
    print(f"  COMPARISON RESULTS  ({elapsed:.1f} min | {len(df)} stocks)")
    print("=" * 65)
    print(f"  {'Metric':<28}  {'WITH TRAIL':>12}  {'NO TRAIL':>12}")
    print("-" * 65)
    print(f"  {'Avg R-multiple (all stocks)':<28}  {overall_a_r:>+12.4f}  {overall_b_r:>+12.4f}")
    print(f"  {'Avg Profit Factor':<28}  {overall_a_pf:>12.3f}  {overall_b_pf:>12.3f}")
    print(f"  {'Stocks where better':<28}  {trail_wins:>11}  {notail_wins:>11}")
    print("=" * 65)

    winner = "WITH TRAILING" if overall_a_r > overall_b_r else "NO TRAILING"
    margin = abs(overall_a_r - overall_b_r)
    print(f"\n  ✅ WINNER: {winner}  (by {margin:.4f} avg R per trade)")

    # ── top stocks per strategy ───────────────────────────────────────────────
    print("\n── Top 10 stocks for TRAIL strategy ─────────────────────────────")
    top_a = df.nlargest(10, "a_avg_r")[
        ["symbol", "trades", "a_win_rate", "a_avg_r", "a_profit_factor"]
    ]
    top_a.columns = ["symbol", "trades", "win%", "avg_r", "PF"]
    print(top_a.to_string(index=False))

    print("\n── Top 10 stocks for NO-TRAIL strategy ──────────────────────────")
    top_b = df.nlargest(10, "b_avg_r")[
        ["symbol", "trades", "b_win_rate", "b_avg_r", "b_profit_factor"]
    ]
    top_b.columns = ["symbol", "trades", "win%", "avg_r", "PF"]
    print(top_b.to_string(index=False))

    # ── stocks that ONLY work with trailing ───────────────────────────────────
    only_trail   = df[(df["a_avg_r"] > 0) & (df["b_avg_r"] <= 0)]
    only_notail  = df[(df["b_avg_r"] > 0) & (df["a_avg_r"] <= 0)]

    print(f"\n── Stocks with edge ONLY in TRAIL ({len(only_trail)}): ", end="")
    print(", ".join(only_trail["symbol"].tolist()) or "none")

    print(f"── Stocks with edge ONLY in NO-TRAIL ({len(only_notail)}): ", end="")
    print(", ".join(only_notail["symbol"].tolist()) or "none")
    print("=" * 65)


if __name__ == "__main__":
    run()
