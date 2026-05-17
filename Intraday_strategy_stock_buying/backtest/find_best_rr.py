"""
Find Optimal Risk:Reward Ratio (No Trailing)
=============================================
Tests multiple TP1 exit ratios on the preferred watchlist stocks.
Full exit at TP1 — no trailing, no partial booking.

Ratios tested: 1:1.5, 1:1.65, 1:1.75, 1:2.0, 1:2.5, 1:3.0

For each ratio:
  Win  → +ratio R
  Loss → -1.0 R
  avg_R = win_rate × ratio − loss_rate × 1.0

Usage:
    cd Intraday_strategy_stock_buying
    python3 backtest/find_best_rr.py
"""

import sys
import time
import logging
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
from Dhan_Tradehull import Tradehull

RESULTS_DIR = ROOT / "backtest" / "results"
RESULTS_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s | %(message)s",
                    handlers=[logging.StreamHandler(sys.stdout)])
logger = logging.getLogger("RR_FINDER")

# ── ratios to test ────────────────────────────────────────────────────────────
RR_RATIOS    = [1.5, 1.65, 1.75, 2.0, 2.5, 3.0]
ENTRY_START  = (9, 30)
ENTRY_END    = (12, 0)
FORCE_EXIT_T = (15, 15)
SLEEP_BTW    = 1.2


def connect():
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


def is_entry_bar(ts):
    from datetime import time as dtime
    t = _naive(ts).time()
    return dtime(*ENTRY_START) <= t < dtime(*ENTRY_END)


def is_force_exit(ts):
    from datetime import time as dtime
    return _naive(ts).time() >= dtime(*FORCE_EXIT_T)


def build_plan(live_df, signal, tp_ratio):
    latest = live_df.iloc[-1]
    buf    = 0.001
    if signal == "LONG":
        entry = float(latest["close"])
        sl    = float(latest["low"]) * (1 - buf)
        risk  = entry - sl
        if risk <= 0:
            return None
        return {"side": "BUY",  "entry": entry, "sl": sl,
                "tp": entry + tp_ratio * risk, "risk": risk}
    elif signal == "SHORT":
        entry = float(latest["close"])
        sl    = float(latest["high"]) * (1 + buf)
        risk  = sl - entry
        if risk <= 0:
            return None
        return {"side": "SELL", "entry": entry, "sl": sl,
                "tp": entry - tp_ratio * risk, "risk": risk}
    return None


def simulate(stock_df, entry_idx, plan, tp_ratio) -> float:
    """Full exit at TP or SL. Returns R-multiple."""
    side  = plan["side"]
    entry = plan["entry"]
    sl    = plan["sl"]
    tp    = plan["tp"]
    risk  = plan["risk"]

    future     = stock_df.iloc[entry_idx + 1:]
    if future.empty:
        return 0.0

    entry_date = _naive(stock_df.index[entry_idx]).date()

    for j in range(len(future)):
        bar = future.iloc[j]
        ts  = _naive(future.index[j])
        hi  = float(bar["high"])
        lo  = float(bar["low"])

        if ts.date() != entry_date or is_force_exit(ts):
            close = float(bar["close"])
            pts   = (close - entry) if side == "BUY" else (entry - close)
            return pts / risk

        if (side == "BUY"  and lo <= sl) or (side == "SELL" and hi >= sl):
            return -1.0

        if (side == "BUY"  and hi >= tp) or (side == "SELL" and lo <= tp):
            return tp_ratio

    close = float(future.iloc[-1]["close"])
    pts   = (close - entry) if side == "BUY" else (entry - close)
    return pts / risk


def analyse_symbol(symbol, stock_df, nifty_df):
    """
    Returns dict: {ratio → list of R-multiples}
    """
    try:
        stock = add_full_indicators(standardize_df(stock_df)).dropna().copy()
        nifty = add_full_indicators(standardize_df(nifty_df)).dropna().copy()
    except:
        return None

    if stock.empty or nifty.empty or len(stock) < 60:
        return None

    # Collect signals first
    signals = []
    max_i   = min(len(stock), len(nifty)) - 5

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

        if signal:
            signals.append((i, signal))

    if not signals:
        return None

    # Run each signal across all ratios
    results = {r: [] for r in RR_RATIOS}

    for i, signal in signals:
        for ratio in RR_RATIOS:
            plan = build_plan(prepare_live_df(stock.iloc[:i + 1]), signal, ratio)
            if not plan:
                continue
            r_mult = simulate(stock, i, plan, ratio)
            results[ratio].append(r_mult)

    return results


def metrics(rs):
    if not rs:
        return {}
    arr  = np.array(rs)
    wins = arr[arr > 0]
    loss = arr[arr <= 0]
    gp   = wins.sum()
    gl   = abs(loss.sum())
    return {
        "trades":    len(arr),
        "win_pct":   round(len(wins) / len(arr) * 100, 1),
        "avg_r":     round(arr.mean(), 4),
        "pf":        round(gp / gl, 2) if gl > 0 else float("inf"),
        "total_r":   round(arr.sum(), 2),
    }


def run():
    # Load preferred watchlist
    wl_path = ROOT / "data" / "preferred_watchlist.csv"
    if not wl_path.exists():
        logger.error("preferred_watchlist.csv not found. Run run_backtest.py first.")
        return

    watchlist = pd.read_csv(wl_path)["symbol"].dropna().tolist()
    logger.info(f"Testing {len(watchlist)} preferred stocks across {len(RR_RATIOS)} RR ratios")
    logger.info(f"Ratios: {RR_RATIOS}")

    tsl = connect()

    logger.info("Fetching NIFTY data...")
    nifty_df = tsl.get_historical_data(tradingsymbol="NIFTY", exchange="INDEX", timeframe="5")
    if nifty_df is None or nifty_df.empty:
        logger.error("NIFTY fetch failed.")
        return

    # Aggregate R-multiples per ratio across all stocks
    all_rs    = {r: [] for r in RR_RATIOS}
    per_stock = []

    for idx, sym in enumerate(watchlist, 1):
        logger.info(f"[{idx}/{len(watchlist)}] {sym}")
        try:
            time.sleep(SLEEP_BTW)
            stock_df = tsl.get_historical_data(tradingsymbol=sym, exchange="NSE", timeframe="5")
            if stock_df is None or stock_df.empty:
                continue

            result = analyse_symbol(sym, stock_df, nifty_df)
            if not result:
                continue

            row = {"symbol": sym}
            for ratio in RR_RATIOS:
                m = metrics(result[ratio])
                if m:
                    all_rs[ratio].extend(result[ratio])
                    row[f"rr{ratio}_avg_r"] = m["avg_r"]
                    row[f"rr{ratio}_wr"]    = m["win_pct"]
                    row[f"rr{ratio}_pf"]    = m["pf"]

            best_ratio = max(RR_RATIOS, key=lambda r: row.get(f"rr{r}_avg_r", -999))
            row["best_ratio"] = best_ratio
            per_stock.append(row)

            # Quick inline summary
            parts = " | ".join(
                f"1:{r} avg_R={row.get(f'rr{r}_avg_r', 0):+.3f}"
                for r in RR_RATIOS
            )
            logger.info(f"        {parts}  ← best: 1:{best_ratio}")

        except Exception as e:
            logger.warning(f"        Error: {e}")

    # ── Overall summary ───────────────────────────────────────────────────────
    print("\n" + "=" * 70)
    print("  OPTIMAL RR RATIO — RESULTS ACROSS ALL PREFERRED STOCKS")
    print("=" * 70)
    print(f"\n  {'Ratio':<10} {'Avg R':>8} {'Win%':>7} {'PF':>6} {'Total R':>9}  {'Verdict'}")
    print("-" * 70)

    ratio_summary = []
    for ratio in RR_RATIOS:
        m = metrics(all_rs[ratio])
        if m:
            ratio_summary.append((ratio, m))
            print(f"  1:{ratio:<8} {m['avg_r']:>+8.4f} {m['win_pct']:>6.1f}% {m['pf']:>6.2f} {m['total_r']:>9.2f}")

    if ratio_summary:
        best = max(ratio_summary, key=lambda x: x[1]["avg_r"])
        print(f"\n  ✅ BEST RATIO: 1:{best[0]}  (avg_R = {best[1]['avg_r']:+.4f})")

    # ── Per-stock best ratio breakdown ────────────────────────────────────────
    if per_stock:
        ps_df = pd.DataFrame(per_stock)

        print(f"\n  Per-stock best ratio:")
        print(f"  {'Stock':<16}", end="")
        for r in RR_RATIOS:
            print(f"  1:{r}", end="")
        print("  ← Best")
        print("  " + "-" * 65)

        for _, row in ps_df.iterrows():
            print(f"  {row['symbol']:<16}", end="")
            for r in RR_RATIOS:
                val = row.get(f"rr{r}_avg_r", float("nan"))
                marker = "★" if r == row["best_ratio"] else " "
                print(f"  {val:>+5.3f}{marker}", end="")
            print()

        # How many stocks prefer each ratio
        print(f"\n  Ratio preference count:")
        counts = ps_df["best_ratio"].value_counts()
        for ratio in RR_RATIOS:
            count = counts.get(ratio, 0)
            bar   = "█" * count
            print(f"  1:{ratio:<8} {bar} {count} stocks")

        # Save
        out = RESULTS_DIR / "rr_comparison.csv"
        ps_df.to_csv(out, index=False)
        print(f"\n  Full data saved → {out}")

    print("=" * 70)


if __name__ == "__main__":
    run()
