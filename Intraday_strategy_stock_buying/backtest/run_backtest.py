"""
Consolidation Breakout Backtest
================================
Tests the 3-state consolidation breakout strategy on NIFTY 50 stocks.

Strategy:
  STATE 1 — Consolidation confirmed:
    Last 5 completed 5-min candles in tight range (≤1%)
    ATR5 < 0.8 × ATR20 (squeeze)
    Price directionally within 0.7% of VWAP

  STATE 2 — Breakout:
    Next completed candle closes beyond the box
    (time gap: must be a NEW candle after STATE 1 confirmation)

  STATE 3 — Pullback entry:
    Candle after breakout: still beyond box + directional VWAP ≤0.8%
    Entry LIMIT at box edge + 0.5%

  Trade management:
    SL = box opposite edge
    TP1 = 1.75× risk (full exit)
    Force exit at 15:15 if neither hit

Usage:
    cd Intraday_strategy_stock_buying
    python3 backtest/run_backtest.py

Output:
    backtest/results/report_YYYYMMDD.csv
"""

import sys
import time
import logging
from datetime import datetime, time as dtime
from pathlib import Path

import numpy as np
import pandas as pd
import talib

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from config.credentials import (
    DHAN_CLIENT_CODE, DHAN_ACCESS_TOKEN, DHAN_PIN, DHAN_TOTP_SECRET,
)
from engine.strategy import standardize_df
from Dhan_Tradehull import Tradehull

# ── output ────────────────────────────────────────────────────────────────────
RESULTS_DIR = ROOT / "backtest" / "results"
RESULTS_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[
        logging.FileHandler(RESULTS_DIR / "backtest.log"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("BACKTEST")

# ── NIFTY 50 universe ─────────────────────────────────────────────────────────
NIFTY_50 = [
    "RELIANCE",  "TCS",       "HDFCBANK",  "BHARTIARTL", "ICICIBANK",
    "INFY",      "SBIN",      "HINDUNILVR","ITC",        "LT",
    "KOTAKBANK", "AXISBANK",  "BAJFINANCE","MARUTI",     "HCLTECH",
    "SUNPHARMA", "TITAN",     "WIPRO",     "ULTRACEMCO", "ASIANPAINT",
    "BAJAJFINSV","NTPC",      "POWERGRID", "TATAMOTORS", "ADANIPORTS",
    "ONGC",      "COALINDIA", "TATASTEEL", "JSWSTEEL",   "INDUSINDBK",
    "GRASIM",    "DRREDDY",   "TECHM",     "CIPLA",      "DIVISLAB",
    "ADANIENT",  "BRITANNIA", "HINDALCO",  "BAJAJ-AUTO", "HEROMOTOCO",
    "EICHERMOT", "TATACONSUM","NESTLEIND", "BPCL",       "IOC",
    "APOLLOHOSP","HDFCLIFE",  "SBILIFE",   "M&M",        "LTIM",
]

# ── strategy parameters ───────────────────────────────────────────────────────
CONSOLIDATION_CANDLES   = 5
CONSOLIDATION_RANGE_PCT = 0.01     # ≤ 1%
ATR_SQUEEZE_RATIO       = 0.8      # ATR5 < 0.8 × ATR20
VWAP_PROXIMITY_PCT      = 0.007    # ≤ 0.7% at consolidation
VWAP_PULLBACK_PCT       = 0.008    # ≤ 0.8% at pullback entry
ENTRY_LIMIT_PCT         = 0.005    # 0.5% beyond box edge
TP1_RR                  = 1.75     # reward:risk ratio

ENTRY_START  = dtime(9, 30)
ENTRY_END    = dtime(14, 30)
FORCE_EXIT   = dtime(15, 15)
SLEEP_BETWEEN = 1.0


# ── broker ────────────────────────────────────────────────────────────────────

def connect() -> Tradehull:
    if DHAN_ACCESS_TOKEN:
        logger.info("Login: access token")
        return Tradehull(DHAN_CLIENT_CODE, DHAN_ACCESS_TOKEN, mode="access_token")
    logger.info("Login: PIN + TOTP")
    return Tradehull(DHAN_CLIENT_CODE, mode="pin_totp",
                     pin=DHAN_PIN, totp_secret=DHAN_TOTP_SECRET)


# ── helpers ───────────────────────────────────────────────────────────────────

def _to_naive(ts):
    if hasattr(ts, "tz_convert") and ts.tz is not None:
        ts = ts.tz_convert("Asia/Kolkata")
    if hasattr(ts, "tz_localize"):
        ts = ts.tz_localize(None)
    return ts


def in_entry_window(ts) -> bool:
    t = _to_naive(ts).time()
    return ENTRY_START <= t < ENTRY_END


def is_force_exit(ts) -> bool:
    return _to_naive(ts).time() >= FORCE_EXIT


def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Add ATR5, ATR20 and session VWAP."""
    df = df.copy()
    df["atr5"]  = talib.ATR(df["high"], df["low"], df["close"], timeperiod=5)
    df["atr20"] = talib.ATR(df["high"], df["low"], df["close"], timeperiod=20)

    # Session VWAP — resets each day
    df["_date"] = pd.to_datetime(df.index).date
    df["_tp"]   = (df["high"] + df["low"] + df["close"]) / 3
    df["_tpv"]  = df["_tp"] * df["volume"]
    df["_cum_tpv"] = df.groupby("_date")["_tpv"].cumsum()
    df["_cum_vol"] = df.groupby("_date")["volume"].cumsum()
    df["vwap"]  = df["_cum_tpv"] / df["_cum_vol"]

    df.drop(columns=["_date", "_tp", "_tpv", "_cum_tpv", "_cum_vol"],
            inplace=True, errors="ignore")
    return df


# ── trade simulation ──────────────────────────────────────────────────────────

def simulate_trade(df: pd.DataFrame, entry_idx: int, entry_price: float,
                   sl: float, tp1: float, side: str, trade_date) -> tuple:
    """
    Walk bars from entry_idx+1.
    Returns (r_multiple, outcome_label).
    Full exit at SL or TP1 — no partial.
    """
    risk = abs(entry_price - sl)
    if risk <= 0:
        return 0.0, "NO_RISK"

    future = df.iloc[entry_idx + 1:]

    for j in range(len(future)):
        bar    = future.iloc[j]
        bar_ts = _to_naive(future.index[j])

        # Force exit at end of day
        if bar_ts.date() != trade_date or is_force_exit(bar_ts):
            close   = float(bar["close"])
            pnl_pts = (close - entry_price) if side == "BUY" else (entry_price - close)
            return round(pnl_pts / risk, 3), "FORCE_EXIT"

        hi = float(bar["high"])
        lo = float(bar["low"])

        # SL check
        if (side == "BUY"  and lo  <= sl) or \
           (side == "SELL" and hi  >= sl):
            return -1.0, "SL_HIT"

        # TP1 check
        if (side == "BUY"  and hi  >= tp1) or \
           (side == "SELL" and lo  <= tp1):
            return round(TP1_RR, 3), "TP1_HIT"

    return 0.0, "END_OF_DATA"


# ── per-symbol backtest ───────────────────────────────────────────────────────

def backtest_symbol(symbol: str, stock_df: pd.DataFrame) -> list:
    """
    Walk 5-min bars, apply 3-state machine, simulate trades.

    Bar indexing at scan point i (treating bar i as last completed):
      Box candles    : bars i-4 to i   (5 bars)
      VWAP/direction : bar i close + VWAP
      confirmed_ts   : bar i timestamp (time gap starts here)
      Breakout bar   : bar i+1  (new candle after confirmation)
      Pullback bar   : bar i+2
      LIMIT fill bar : bar i+3  (fills if price hits box edge ± 0.5%)
      Trade sim      : from bar i+4 onwards
    """
    try:
        df = standardize_df(stock_df).copy()
        df = add_indicators(df).dropna()
    except Exception as e:
        return []

    N = len(df)
    C = CONSOLIDATION_CANDLES  # 5
    if N < C + 6:
        return []

    trades = []
    i = C  # start with enough history

    while i < N - 5:
        bar_ts = _to_naive(df.index[i])

        if not in_entry_window(bar_ts):
            i += 1
            continue

        # ── STATE 1: Consolidation ──────────────────────────────
        box = df.iloc[i - C + 1: i + 1]   # bars i-4 to i (inclusive) = 5 bars
        box_high = float(box["high"].max())
        box_low  = float(box["low"].min())

        if box_low <= 0:
            i += 1
            continue

        range_pct = (box_high - box_low) / box_low
        if range_pct > CONSOLIDATION_RANGE_PCT:
            i += 1
            continue

        atr5  = float(df["atr5"].iloc[i])
        atr20 = float(df["atr20"].iloc[i])
        if pd.isna(atr5) or pd.isna(atr20) or atr20 == 0:
            i += 1
            continue
        if atr5 >= ATR_SQUEEZE_RATIO * atr20:
            i += 1
            continue

        close_i = float(df["close"].iloc[i])
        vwap_i  = float(df["vwap"].iloc[i])

        if close_i >= vwap_i:
            diff = (close_i - vwap_i) / vwap_i
            if not (0 <= diff <= VWAP_PROXIMITY_PCT):
                i += 1
                continue
            direction = "LONG"
        else:
            diff = (vwap_i - close_i) / vwap_i
            if not (0 <= diff <= VWAP_PROXIMITY_PCT):
                i += 1
                continue
            direction = "SHORT"

        # ── STATE 2: Breakout at bar i+1 (time gap enforced) ────
        if i + 1 >= N:
            i += 1
            continue

        bo_bar   = df.iloc[i + 1]
        bo_ts    = _to_naive(df.index[i + 1])
        bo_close = float(bo_bar["close"])

        if bo_ts.date() != bar_ts.date() or not in_entry_window(bo_ts):
            i += 1
            continue

        if direction == "LONG"  and bo_close <= box_high:
            i += 1
            continue
        if direction == "SHORT" and bo_close >= box_low:
            i += 1
            continue

        # ── STATE 3: Pullback at bar i+2 ─────────────────────────
        if i + 2 >= N:
            i += 1
            continue

        pb_bar   = df.iloc[i + 2]
        pb_ts    = _to_naive(df.index[i + 2])
        pb_close = float(pb_bar["close"])
        pb_vwap  = float(pb_bar["vwap"])

        if pb_ts.date() != bar_ts.date() or not in_entry_window(pb_ts):
            i += 1
            continue

        if direction == "LONG":
            if pb_close <= box_high:
                i += 1
                continue  # back in box
            pb_diff = (pb_close - pb_vwap) / pb_vwap if pb_vwap > 0 else 1.0
            if not (0 <= pb_diff <= VWAP_PULLBACK_PCT):
                i += 1
                continue
        else:
            if pb_close >= box_low:
                i += 1
                continue  # back in box
            pb_diff = (pb_vwap - pb_close) / pb_vwap if pb_vwap > 0 else 1.0
            if not (0 <= pb_diff <= VWAP_PULLBACK_PCT):
                i += 1
                continue

        # ── LIMIT fill at bar i+3 ─────────────────────────────────
        if i + 3 >= N:
            i += 1
            continue

        fill_bar = df.iloc[i + 3]
        fill_ts  = _to_naive(df.index[i + 3])

        if fill_ts.date() != bar_ts.date():
            i += 1
            continue

        if direction == "LONG":
            entry = box_high * (1 + ENTRY_LIMIT_PCT)
            sl    = box_low  * (1 - 0.001)
            side  = "BUY"
            if float(fill_bar["low"]) > entry:
                i += 1
                continue  # LIMIT didn't fill
        else:
            entry = box_low  * (1 - ENTRY_LIMIT_PCT)
            sl    = box_high * (1 + 0.001)
            side  = "SELL"
            if float(fill_bar["high"]) < entry:
                i += 1
                continue  # LIMIT didn't fill

        risk = abs(entry - sl)
        if risk <= 0:
            i += 1
            continue

        tp1 = (entry + TP1_RR * risk) if direction == "LONG" else (entry - TP1_RR * risk)

        r_mult, outcome = simulate_trade(df, i + 3, entry, sl, tp1, side, bar_ts.date())

        trades.append({
            "symbol":    symbol,
            "direction": direction,
            "date":      str(bar_ts.date()),
            "entry_ts":  str(df.index[i + 3]),
            "entry":     round(entry, 2),
            "sl":        round(sl, 2),
            "tp1":       round(tp1, 2),
            "box_high":  round(box_high, 2),
            "box_low":   round(box_low, 2),
            "range_pct": round(range_pct * 100, 2),
            "atr_ratio": round(atr5 / atr20, 3),
            "r_mult":    r_mult,
            "outcome":   outcome,
        })

        i += 5  # skip past fill bar to avoid overlapping setups

    return trades


# ── main ──────────────────────────────────────────────────────────────────────

def run():
    start_wall = time.time()
    logger.info("=" * 62)
    logger.info(f"  CONSOLIDATION BREAKOUT BACKTEST — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    logger.info(f"  Universe: {len(NIFTY_50)} stocks (NIFTY 50) | Timeframe: 5-min")
    logger.info("=" * 62)

    tsl = connect()

    all_trades = []
    sym_stats  = []
    skipped    = []

    for idx, sym in enumerate(NIFTY_50, 1):
        logger.info(f"[{idx:>2}/{len(NIFTY_50)}] {sym:<16}", )
        try:
            time.sleep(SLEEP_BETWEEN)
            df = tsl.get_historical_data(tradingsymbol=sym, exchange="NSE", timeframe="5")
            if df is None or df.empty or len(df) < 60:
                logger.info(f"        → skipped (no data)")
                skipped.append(sym)
                continue

            trades = backtest_symbol(sym, df)
            if not trades:
                logger.info(f"        → 0 setups found")
                sym_stats.append({"symbol": sym, "trades": 0, "win_rate": 0,
                                   "avg_r": 0, "profit_factor": 0,
                                   "long_trades": 0, "short_trades": 0})
                continue

            all_trades.extend(trades)
            tdf  = pd.DataFrame(trades)
            wins = tdf[tdf["r_mult"] > 0]
            loss = tdf[tdf["r_mult"] <= 0]
            gp   = wins["r_mult"].sum()
            gl   = abs(loss["r_mult"].sum())
            pf   = round(gp / gl, 2) if gl > 0 else float("inf")
            wr   = round(len(wins) / len(tdf) * 100, 1)
            ar   = round(tdf["r_mult"].mean(), 3)

            sym_stats.append({
                "symbol":       sym,
                "trades":       len(tdf),
                "win_rate":     wr,
                "avg_r":        ar,
                "profit_factor": pf,
                "long_trades":  int((tdf["direction"] == "LONG").sum()),
                "short_trades": int((tdf["direction"] == "SHORT").sum()),
            })

            logger.info(
                f"        → {len(tdf):>3} trades | "
                f"WR {wr:>5.1f}% | avg_R {ar:>+.3f} | PF {pf:>4.2f}"
            )

        except Exception as e:
            logger.warning(f"        → error: {e}")
            skipped.append(sym)

    # ── save full report ──────────────────────────────────────────────────────
    if not all_trades:
        logger.error("No trades found across all symbols.")
        return

    report_path = RESULTS_DIR / f"report_{datetime.now().strftime('%Y%m%d')}.csv"
    pd.DataFrame(all_trades).to_csv(report_path, index=False)
    logger.info(f"\nFull trade log saved → {report_path}")

    stats_df = pd.DataFrame(sym_stats).sort_values("avg_r", ascending=False)

    # ── print summary ─────────────────────────────────────────────────────────
    elapsed = (time.time() - start_wall) / 60
    all_df  = pd.DataFrame(all_trades)
    total_t = len(all_df)
    wins_t  = len(all_df[all_df["r_mult"] > 0])
    avg_r_t = round(all_df["r_mult"].mean(), 3)
    gp_t    = all_df[all_df["r_mult"] > 0]["r_mult"].sum()
    gl_t    = abs(all_df[all_df["r_mult"] <= 0]["r_mult"].sum())
    pf_t    = round(gp_t / gl_t, 2) if gl_t > 0 else float("inf")

    print("\n" + "=" * 62)
    print(f"  BACKTEST COMPLETE  ({elapsed:.1f} min)")
    print("=" * 62)
    print(f"  Universe    : {len(NIFTY_50)} stocks | Skipped: {len(skipped)}")
    print(f"  Total trades: {total_t}")
    print(f"  Win rate    : {wins_t/total_t*100:.1f}%")
    print(f"  Avg R       : {avg_r_t:+.3f}")
    print(f"  Profit factor: {pf_t:.2f}")
    print()

    long_df  = all_df[all_df["direction"] == "LONG"]
    short_df = all_df[all_df["direction"] == "SHORT"]
    if not long_df.empty:
        print(f"  LONG  trades: {len(long_df)} | WR:{len(long_df[long_df['r_mult']>0])/len(long_df)*100:.1f}% | avg_R:{long_df['r_mult'].mean():+.3f}")
    if not short_df.empty:
        print(f"  SHORT trades: {len(short_df)} | WR:{len(short_df[short_df['r_mult']>0])/len(short_df)*100:.1f}% | avg_R:{short_df['r_mult'].mean():+.3f}")

    print()
    print(f"  Outcome breakdown:")
    for outcome, cnt in all_df["outcome"].value_counts().items():
        print(f"    {outcome:<15}: {cnt:>4} ({cnt/total_t*100:.1f}%)")

    print()
    print(f"{'#':<3} {'SYMBOL':<14} {'TRADES':>6} {'WR%':>6} {'AVG_R':>7} {'PF':>6}")
    print("-" * 44)
    for rank, (_, row) in enumerate(stats_df[stats_df["trades"] > 0].head(20).iterrows(), 1):
        print(
            f"{rank:<3} {row['symbol']:<14} "
            f"{int(row['trades']):>6} "
            f"{row['win_rate']:>5.1f}% "
            f"{row['avg_r']:>+7.3f} "
            f"{row['profit_factor']:>6.2f}"
        )
    print("=" * 62)


if __name__ == "__main__":
    run()
