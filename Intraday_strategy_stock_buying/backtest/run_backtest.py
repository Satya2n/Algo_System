"""
Dual-Timeframe Consolidation Breakout Backtest
===============================================
Mirrors the live system exactly:

  STEP 1 — 15-min score filter (like the live scanner)
    rank_stock on 15-min data at each point in time
    Score > 70 AND vol >= 4 → stock qualifies for entry

  STEP 2 — 5-min consolidation check
    Last 5 completed 5-min candles:
      Range <= 1%
      ATR5 < 0.8 x ATR20
      Price directionally within 0.7% of VWAP (LONG above, SHORT below)

  STEP 3 — 5-min breakout entry
    Current 5-min candle closes beyond the box
    Enter at that candle's close price
    SL = box opposite edge
    TP1 = 1.75 x risk

Usage:
    cd Intraday_strategy_stock_buying
    python3 backtest/run_backtest.py
"""

import sys
import time
import logging
from datetime import datetime, time as dtime
from pathlib import Path

import pandas as pd
import talib

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from config.credentials import (
    DHAN_CLIENT_CODE, DHAN_ACCESS_TOKEN, DHAN_PIN, DHAN_TOTP_SECRET,
)
from engine.strategy import standardize_df, rank_stock
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

# ── 10 stocks to test ─────────────────────────────────────────────────────────
STOCKS = [
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
SCORE_THRESHOLD         = 70
MIN_VOL_SCORE           = 4
CONSOLIDATION_CANDLES   = 5
CONSOLIDATION_RANGE_PCT = 0.015    # ≤ 1.5%
ATR_SQUEEZE_RATIO       = 0.8
VWAP_PROXIMITY_PCT      = 0.007
TP1_RR                  = 1.75

ENTRY_START = dtime(9, 45)   # 9:15 opening candle excluded from box
ENTRY_END   = dtime(14, 30)
FORCE_EXIT  = dtime(15, 15)
SLEEP       = 0.8

# ── Sector index IDs (direct Dhan API) ───────────────────────────────────────
SECTOR_IDS = {
    "NIFTY AUTO":    14,
    "NIFTY IT":      29,
    "NIFTY FMCG":    28,
    "NIFTY METAL":   31,
    "NIFTY PSU BANK": 33,
    "NIFTY PHARMA": 32,   # covers pharma + healthcare
}

# Tradehull-supported indices (fetched via get_historical_data)
TRADEHULL_INDICES = {
    "BANKNIFTY": "BANKNIFTY",
    "FINNIFTY":  "FINNIFTY",
    "NIFTY":     "NIFTY",
}

# Stock → sector benchmark mapping
STOCK_SECTOR = {
    # Auto
    "MARUTI": "NIFTY AUTO", "HEROMOTOCO": "NIFTY AUTO",
    "BAJAJ-AUTO": "NIFTY AUTO", "EICHERMOT": "NIFTY AUTO",
    "M&M": "NIFTY AUTO", "TATAMOTORS": "NIFTY AUTO",
    # IT
    "TCS": "NIFTY IT", "INFY": "NIFTY IT", "WIPRO": "NIFTY IT",
    "HCLTECH": "NIFTY IT", "TECHM": "NIFTY IT",
    # FMCG
    "HINDUNILVR": "NIFTY FMCG", "ITC": "NIFTY FMCG",
    "NESTLEIND": "NIFTY FMCG", "BRITANNIA": "NIFTY FMCG",
    "TATACONSUM": "NIFTY FMCG",
    # Metal
    "TATASTEEL": "NIFTY METAL", "JSWSTEEL": "NIFTY METAL",
    "HINDALCO": "NIFTY METAL", "COALINDIA": "NIFTY METAL",
    # PSU Bank
    "SBIN": "NIFTY PSU BANK",
    # Pharma
    "SUNPHARMA": "NIFTY PHARMA", "DRREDDY": "NIFTY PHARMA",
    "CIPLA": "NIFTY PHARMA", "DIVISLAB": "NIFTY PHARMA",
    # Private Banks → BANKNIFTY
    "HDFCBANK": "BANKNIFTY", "ICICIBANK": "BANKNIFTY",
    "KOTAKBANK": "BANKNIFTY", "AXISBANK": "BANKNIFTY",
    "INDUSINDBK": "BANKNIFTY", "BAJFINANCE": "BANKNIFTY",
    "BAJAJFINSV": "BANKNIFTY",
    # Financial Services → FINNIFTY
    "SBILIFE": "FINNIFTY", "HDFCLIFE": "FINNIFTY",
    # Healthcare → NIFTY PHARMA (same index)
    "APOLLOHOSP": "NIFTY PHARMA",
    # All others → NIFTY 50 (default)
}


# ── helpers ───────────────────────────────────────────────────────────────────

def connect() -> Tradehull:
    if DHAN_ACCESS_TOKEN:
        return Tradehull(DHAN_CLIENT_CODE, DHAN_ACCESS_TOKEN, mode="access_token")
    return Tradehull(DHAN_CLIENT_CODE, mode="pin_totp",
                     pin=DHAN_PIN, totp_secret=DHAN_TOTP_SECRET)


def get_sector_data(tsl: Tradehull, sector: str, interval: int = 25) -> pd.DataFrame:
    """
    Fetch sector index 25-min data.
    Uses direct Dhan API for sector indices not supported by Tradehull.
    """
    from datetime import timedelta
    # Tradehull-supported indices
    if sector in TRADEHULL_INDICES:
        sym = TRADEHULL_INDICES[sector]
        df = tsl.get_historical_data(tradingsymbol=sym, exchange="INDEX", timeframe=str(interval))
        return df if df is not None and not df.empty else pd.DataFrame()

    # Direct Dhan API for sector indices
    if sector in SECTOR_IDS:
        sec_id = SECTOR_IDS[sector]
        end_dt   = datetime.now()
        start_dt = end_dt - timedelta(days=90)
        try:
            ohlc = tsl.Dhan.intraday_minute_data(
                str(sec_id), tsl.Dhan.INDEX, "INDEX",
                start_dt.strftime("%Y-%m-%d"),
                end_dt.strftime("%Y-%m-%d"),
                interval, oi=False
            )
            if not ohlc or ohlc.get("status") == "failure":
                return pd.DataFrame()
            df = pd.DataFrame(ohlc["data"])
            if df.empty or "timestamp" not in df.columns:
                return pd.DataFrame()
            df["timestamp"] = (pd.to_datetime(df["timestamp"], unit="s", utc=True)
                               .dt.tz_convert("Asia/Kolkata").dt.tz_localize(None))
            df.set_index("timestamp", inplace=True)
            df.index = pd.to_datetime(df.index)   # ensure pandas Timestamp (not datetime64[s])
            df = df[["open", "high", "low", "close", "volume"]].sort_index()
            return df
        except Exception as e:
            logger.warning(f"Sector data fetch failed for {sector}: {e}")
            return pd.DataFrame()

    # Default: NIFTY 50
    df = tsl.get_historical_data(tradingsymbol="NIFTY", exchange="INDEX", timeframe=str(interval))
    return df if df is not None and not df.empty else pd.DataFrame()


def _to_naive(ts):
    if hasattr(ts, "tz_convert") and ts.tz is not None:
        ts = ts.tz_convert("Asia/Kolkata")
    if hasattr(ts, "tz_localize"):
        ts = ts.tz_localize(None)
    return ts


def in_entry_window(ts) -> bool:
    return ENTRY_START <= _to_naive(ts).time() < ENTRY_END


def is_force_exit(ts) -> bool:
    return _to_naive(ts).time() >= FORCE_EXIT


def add_5min_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """ATR5, ATR20, session VWAP on 5-min data."""
    df = df.copy()
    df["atr5"]  = talib.ATR(df["high"], df["low"], df["close"], timeperiod=5)
    df["atr20"] = talib.ATR(df["high"], df["low"], df["close"], timeperiod=20)
    df["_date"] = pd.to_datetime(df.index).date
    df["_tp"]   = (df["high"] + df["low"] + df["close"]) / 3
    df["_tpv"]  = df["_tp"] * df["volume"]
    df["_cumtpv"] = df.groupby("_date")["_tpv"].cumsum()
    df["_cumvol"] = df.groupby("_date")["volume"].cumsum()
    df["vwap"]  = df["_cumtpv"] / df["_cumvol"]
    df.drop(columns=["_date", "_tp", "_tpv", "_cumtpv", "_cumvol"],
            inplace=True, errors="ignore")
    return df


def build_15min_score_map(df15: pd.DataFrame, nifty15: pd.DataFrame) -> dict:
    """
    Pre-compute rank_stock at every 15-min bar.
    Returns dict: { timestamp_str → rank_result_dict }
    Expensive to compute on-the-fly — do it once.
    """
    scores = {}
    # normalize_df: any source → naive IST datetime64[ns], safe to compare
    stock  = normalize_df(standardize_df(df15).copy())
    nifty  = normalize_df(standardize_df(nifty15).copy())

    for i in range(30, len(stock)):
        ts       = stock.index[i]   # naive IST Timestamp
        s_slice  = stock.iloc[:i + 1]
        n_slice  = nifty[nifty.index <= ts].copy()
        if n_slice.empty:
            continue
        try:
            r = rank_stock(s_slice, n_slice)
            if r:
                scores[ts] = r
        except Exception:
            pass

    return scores


def normalize_df(df: pd.DataFrame) -> pd.DataFrame:
    """
    Normalise any DataFrame index to naive IST nanoseconds.
    Handles both sources:
      - Tradehull data  : datetime64[us, UTC+05:30]  → strip TZ → naive IST
      - Direct Dhan API : datetime64[s] naive          → convert to datetime64[ns]
    After this, all DataFrames have the same dtype and no timezone — safe to compare.
    """
    df = df.copy()
    idx = pd.to_datetime(df.index)
    if idx.tz is not None:
        idx = idx.tz_convert('Asia/Kolkata').tz_localize(None)
    df.index = idx.astype('datetime64[ns]')
    return df


def _ts_naive(ts):
    """Strip timezone from a single Timestamp, keeping IST wall-clock time."""
    ts = pd.Timestamp(ts)
    if ts.tz is not None:
        ts = ts.tz_convert('Asia/Kolkata').tz_localize(None)
    return ts


def get_15min_score_at(ts, score_map: dict):
    """
    Find the most recent 25-min score at or before ts.
    """
    ts = _ts_naive(ts)
    candidates = [k for k in score_map if k <= ts]
    if not candidates:
        return None
    return score_map[max(candidates)]


def build_nifty_vwap(nifty5: pd.DataFrame) -> pd.DataFrame:
    """Compute NIFTY session VWAP on 5-min bars — resets each day."""
    df = standardize_df(nifty5).copy()
    df["_date"]   = pd.to_datetime(df.index).date
    df["_tp"]     = (df["high"] + df["low"] + df["close"]) / 3
    df["_tpv"]    = df["_tp"] * df["volume"]
    df["_cumtpv"] = df.groupby("_date")["_tpv"].cumsum()
    df["_cumvol"] = df.groupby("_date")["volume"].cumsum()
    df["vwap"]    = df["_cumtpv"] / df["_cumvol"]
    df.drop(columns=["_date", "_tp", "_tpv", "_cumtpv", "_cumvol"],
            inplace=True, errors="ignore")
    return df


def nifty_aligned(nifty_vwap: pd.DataFrame, ts, direction: str) -> bool:
    """
    NIFTY regime filter — only enter when NIFTY confirms direction.
    LONG  : NIFTY close > NIFTY session VWAP (market is bullish)
    SHORT : NIFTY close < NIFTY session VWAP (market is bearish)
    """
    candidates = nifty_vwap[nifty_vwap.index <= _ts_naive(ts)]
    if candidates.empty:
        return True  # no data — don't block the trade
    bar   = candidates.iloc[-1]
    close = float(bar["close"])
    vwap  = float(bar["vwap"])
    if direction == "LONG":
        return close > vwap
    return close < vwap


# ── trade simulation ──────────────────────────────────────────────────────────

def simulate_trade(df5: pd.DataFrame, entry_idx: int, entry: float,
                   sl: float, side: str, trade_date) -> tuple:
    """
    1:1 TP with trail to breakeven:

    Phase 1 — SL or TP1 (1x risk):
      SL hit before TP1  → -1.0R
      TP1 (1x) hit first → book 50%, move SL to breakeven → Phase 2

    Phase 2 — remaining 50% with SL at entry (breakeven):
      BE SL hit   → +0.5R  (50% at 1R, 50% at 0R)
      Force exit  → +0.5R + 0.5 × (exit_R)
    """
    risk = abs(entry - sl)
    if risk <= 0:
        return 0.0, "NO_RISK"

    tp1_1x   = (entry + risk) if side == "BUY" else (entry - risk)
    trail_sl = sl
    partial  = False

    future = df5.iloc[entry_idx + 1:]

    for j in range(len(future)):
        bar = future.iloc[j]
        ts  = _to_naive(future.index[j])

        if ts.date() != trade_date or is_force_exit(ts):
            close   = float(bar["close"])
            raw_r   = ((close - entry) if side == "BUY" else (entry - close)) / risk
            if not partial:
                return round(raw_r, 3), "FORCE_EXIT"
            else:
                return round(0.5 * 1.0 + 0.5 * raw_r, 3), "TP1+FORCE_EXIT"

        hi, lo = float(bar["high"]), float(bar["low"])

        if not partial:
            if (side == "BUY" and lo <= trail_sl) or (side == "SELL" and hi >= trail_sl):
                return -1.0, "SL_HIT"
            if (side == "BUY" and hi >= tp1_1x) or (side == "SELL" and lo <= tp1_1x):
                partial  = True
                trail_sl = entry   # move SL to breakeven
        else:
            if (side == "BUY" and lo <= trail_sl) or (side == "SELL" and hi >= trail_sl):
                return round(0.5 * 1.0, 3), "TP1+BE_SL"

    return 0.0, "END"


# ── per-symbol backtest ───────────────────────────────────────────────────────

def backtest_symbol(sym: str, df5: pd.DataFrame, df15: pd.DataFrame,
                    sector15: pd.DataFrame, nifty_vwap: pd.DataFrame) -> list:
    """
    Walk every 5-min bar.
    At each bar:
      1. Check 15-min score (gate)
      2. Check 5-min consolidation (range, ATR, VWAP)
      3. Check if current bar broke out of box
      4. Enter at close, simulate forward
    """
    try:
        stock5 = normalize_df(standardize_df(df5).copy())
        stock5 = add_5min_indicators(stock5).dropna()
    except Exception as e:
        logger.warning(f"[{sym}] prep error: {e}")
        return []

    logger.info(f"[{sym}] Building 25-min score map...")
    score_map = build_15min_score_map(df15, sector15)
    if not score_map:
        logger.warning(f"[{sym}] No 15-min scores computed")
        return []

    N = len(stock5)
    C = CONSOLIDATION_CANDLES
    if N < C + 3:
        return []

    trades  = []
    i       = C + 1

    while i < N - 2:
        bar_ts = _ts_naive(stock5.index[i])

        if not in_entry_window(bar_ts):
            i += 1
            continue

        # ── STEP 1: 15-min score gate ─────────────────────────────
        score_r = get_15min_score_at(bar_ts, score_map)
        if not score_r:
            i += 1
            continue

        ls  = score_r["long_score"]
        ss  = score_r["short_score"]
        vol = score_r["volume_score"]
        dec = score_r["decision"]

        if vol < MIN_VOL_SCORE:
            i += 1
            continue

        if dec == "LONG" and ls < SCORE_THRESHOLD:
            i += 1
            continue
        if dec == "SHORT" and ss < SCORE_THRESHOLD:
            i += 1
            continue
        if dec == "SKIP":
            i += 1
            continue

        direction = dec  # "LONG" or "SHORT"

        # ── STEP 2: 5-min consolidation ───────────────────────────
        # Box = last 5 completed bars (i-5 to i-1)
        box  = stock5.iloc[i - C: i]
        bh   = float(box["high"].max())
        bl   = float(box["low"].min())

        if bl <= 0:
            i += 1
            continue

        rng = (bh - bl) / bl
        if rng > CONSOLIDATION_RANGE_PCT:
            i += 1
            continue

        atr5  = float(stock5["atr5"].iloc[i])
        atr20 = float(stock5["atr20"].iloc[i])
        if pd.isna(atr5) or pd.isna(atr20) or atr20 == 0:
            i += 1
            continue
        if atr5 >= ATR_SQUEEZE_RATIO * atr20:
            i += 1
            continue

        close_i = float(stock5["close"].iloc[i])
        vwap_i  = float(stock5["vwap"].iloc[i])

        if direction == "LONG":
            vd = (close_i - vwap_i) / vwap_i if vwap_i > 0 else 1.0
            if not (0 <= vd <= VWAP_PROXIMITY_PCT):
                i += 1
                continue
        else:
            vd = (vwap_i - close_i) / vwap_i if vwap_i > 0 else 1.0
            if not (0 <= vd <= VWAP_PROXIMITY_PCT):
                i += 1
                continue

        # ── STEP 3: Breakout on bar i ──────────────────────────────
        if direction == "LONG" and close_i <= bh:
            i += 1
            continue
        if direction == "SHORT" and close_i >= bl:
            i += 1
            continue

        # NIFTY regime filter — only enter when NIFTY confirms direction
        if not nifty_aligned(nifty_vwap, bar_ts, direction):
            i += 1
            continue

        # Breakout confirmed — enter at close of breakout bar
        # 1:1 TP then trail SL to breakeven
        if direction == "LONG":
            entry = close_i
            sl    = bl * 0.999
            side  = "BUY"
        else:
            entry = close_i
            sl    = bh * 1.001
            side  = "SELL"

        risk = abs(entry - sl)
        if risk <= 0:
            i += 1
            continue

        trade_date = _to_naive(bar_ts).date()
        r_mult, outcome = simulate_trade(stock5, i, entry, sl, side, trade_date)

        trades.append({
            "symbol":    sym,
            "direction": direction,
            "date":      str(trade_date),
            "entry_ts":  str(bar_ts),
            "entry":     round(entry, 2),
            "sl":        round(sl, 2),
            "tp1_1x":    round(entry + risk if side == "BUY" else entry - risk, 2),
            "box_high":  round(bh, 2),
            "box_low":   round(bl, 2),
            "range_pct": round(rng * 100, 2),
            "atr_ratio": round(atr5 / atr20, 3),
            "score_l":   round(ls, 1),
            "score_s":   round(ss, 1),
            "vol_score": vol,
            "r_mult":    r_mult,
            "outcome":   outcome,
        })

        i += 4  # skip forward to avoid overlapping setups

    return trades


# ── main ──────────────────────────────────────────────────────────────────────

def run():
    t0 = time.time()
    logger.info("=" * 62)
    logger.info(f"  DUAL-TIMEFRAME BACKTEST — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    logger.info(f"  60-min score gate  | 5-min consolidation + breakout entry")
    logger.info(f"  Stocks: {len(STOCKS)} | Score threshold: {SCORE_THRESHOLD}")
    logger.info("=" * 62)

    tsl = connect()

    # ── fetch all sector indices (25-min) ─────────────────────────
    logger.info("Fetching sector index data (25-min)...")
    all_sectors = set(STOCK_SECTOR.values()) | {"NIFTY"}
    sector_cache: dict = {}

    for sec_name in sorted(all_sectors):
        time.sleep(0.5)
        df = get_sector_data(tsl, sec_name, interval=60)
        if df is not None and not df.empty:
            sector_cache[sec_name] = normalize_df(df)
            logger.info(f"  {sec_name:<20} {len(df)} bars")
        else:
            logger.warning(f"  {sec_name:<20} FAILED — falling back to NIFTY")

    nifty_default = sector_cache.get("NIFTY", pd.DataFrame())
    if nifty_default.empty:
        logger.error("No NIFTY data. Abort.")
        return

    # ── NIFTY 5-min for regime filter ─────────────────────────────
    logger.info("Fetching NIFTY 5-min data (regime filter)...")
    nifty5_raw = tsl.get_historical_data("NIFTY", "INDEX", "5")
    if nifty5_raw is None or nifty5_raw.empty:
        logger.error("No NIFTY 5-min data. Abort.")
        return
    nifty_vwap_df = normalize_df(build_nifty_vwap(nifty5_raw))
    logger.info(f"NIFTY 5-min VWAP built: {len(nifty_vwap_df)} bars")

    all_trades = []
    sym_stats  = []

    for idx, sym in enumerate(STOCKS, 1):
        logger.info(f"\n[{idx}/{len(STOCKS)}] {sym}")
        try:
            time.sleep(SLEEP)
            df5 = tsl.get_historical_data(sym, "NSE", "5")
            if df5 is None or df5.empty or len(df5) < 60:
                logger.info(f"  → skipped (no 5-min data)")
                continue

            time.sleep(SLEEP)
            df15 = tsl.get_historical_data(sym, "NSE", "60")
            if df15 is None or df15.empty or len(df15) < 20:
                logger.info(f"  → skipped (no 25-min data)")
                continue

            df5  = normalize_df(df5)
            df15 = normalize_df(df15)
            logger.info(f"  5-min: {len(df5)} bars | 25-min: {len(df15)} bars")

            # Use stock's sector index for scoring, fallback to NIFTY
            sector_name = STOCK_SECTOR.get(sym, "NIFTY")
            sector_df   = sector_cache.get(sector_name, nifty_default)
            logger.info(f"  Sector: {sector_name}")

            trades = backtest_symbol(sym, df5, df15, sector_df, nifty_vwap_df)

            if not trades:
                logger.info(f"  → 0 qualifying setups found")
                sym_stats.append({"symbol": sym, "trades": 0, "win_rate": 0,
                                   "avg_r": 0, "profit_factor": 0})
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
                "symbol":        sym,
                "trades":        len(tdf),
                "win_rate":      wr,
                "avg_r":         ar,
                "profit_factor": pf,
                "long_trades":   int((tdf["direction"] == "LONG").sum()),
                "short_trades":  int((tdf["direction"] == "SHORT").sum()),
            })

            logger.info(
                f"  → {len(tdf)} trades | WR {wr:.1f}% | avg_R {ar:+.3f} | PF {pf:.2f}"
            )
            for out, cnt in tdf["outcome"].value_counts().items():
                logger.info(f"     {out}: {cnt} ({cnt/len(tdf)*100:.0f}%)")

        except Exception as e:
            logger.warning(f"  → error: {e}")

    # ── report ────────────────────────────────────────────────────
    if not all_trades:
        logger.error("No trades found.")
        return

    out_path = RESULTS_DIR / f"report_{datetime.now().strftime('%Y%m%d')}.csv"
    pd.DataFrame(all_trades).to_csv(out_path, index=False)
    logger.info(f"\nTrade log saved → {out_path}")

    adf  = pd.DataFrame(all_trades)
    tot  = len(adf)
    wins = len(adf[adf["r_mult"] > 0])
    ar   = round(adf["r_mult"].mean(), 3)
    gp   = adf[adf["r_mult"] > 0]["r_mult"].sum()
    gl   = abs(adf[adf["r_mult"] <= 0]["r_mult"].sum())
    pf   = round(gp / gl, 2) if gl > 0 else float("inf")

    elapsed = (time.time() - t0) / 60
    print("\n" + "=" * 62)
    print(f"  BACKTEST COMPLETE  ({elapsed:.1f} min)")
    print("=" * 62)
    print(f"  Stocks tested  : {len(STOCKS)}")
    print(f"  Total trades   : {tot}")
    print(f"  Win rate       : {wins/tot*100:.1f}%")
    print(f"  Avg R          : {ar:+.3f}")
    print(f"  Profit factor  : {pf:.2f}")
    print()

    long_df  = adf[adf["direction"] == "LONG"]
    short_df = adf[adf["direction"] == "SHORT"]
    if not long_df.empty:
        lw = len(long_df[long_df["r_mult"] > 0])
        print(f"  LONG  : {len(long_df)} trades | WR {lw/len(long_df)*100:.1f}% | avg_R {long_df['r_mult'].mean():+.3f}")
    if not short_df.empty:
        sw = len(short_df[short_df["r_mult"] > 0])
        print(f"  SHORT : {len(short_df)} trades | WR {sw/len(short_df)*100:.1f}% | avg_R {short_df['r_mult'].mean():+.3f}")

    print()
    print("  Outcome breakdown:")
    for out, cnt in adf["outcome"].value_counts().items():
        print(f"    {out:<15}: {cnt:>4}  ({cnt/tot*100:.1f}%)")

    print()
    stats_df = pd.DataFrame(sym_stats).sort_values("avg_r", ascending=False)
    print(f"  {'SYMBOL':<14} {'TRADES':>6} {'WR%':>6} {'AVG_R':>7} {'PF':>6}")
    print("  " + "-" * 42)
    for _, row in stats_df[stats_df["trades"] > 0].iterrows():
        print(
            f"  {row['symbol']:<14} {int(row['trades']):>6} "
            f"{row['win_rate']:>5.1f}% {row['avg_r']:>+7.3f} "
            f"{row['profit_factor']:>6.2f}"
        )
    print("=" * 62)


if __name__ == "__main__":
    run()
