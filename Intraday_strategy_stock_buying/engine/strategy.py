import pandas as pd
import numpy as np
import talib
from datetime import time


# =========================
# Data standardization
# =========================

def standardize_df(df: pd.DataFrame) -> pd.DataFrame:
    """
    Standardize OHLCV dataframe:
    - set timestamp/datetime/date as index if present
    - sort index
    - lowercase columns
    """
    if df is None:
        return pd.DataFrame()

    df = df.copy()

    if "timestamp" in df.columns:
        df["timestamp"] = pd.to_datetime(df["timestamp"])
        df = df.set_index("timestamp")
    elif "datetime" in df.columns:
        df["datetime"] = pd.to_datetime(df["datetime"])
        df = df.set_index("datetime")
    elif "date" in df.columns:
        df["date"] = pd.to_datetime(df["date"])
        df = df.set_index("date")
    else:
        if not isinstance(df.index, pd.DatetimeIndex):
            raise ValueError(
                "No timestamp/datetime/date column found and index is not DatetimeIndex."
            )

    df = df.sort_index()
    df.columns = [str(c).lower() for c in df.columns]
    return df


def get_today_session(df: pd.DataFrame) -> pd.DataFrame:
    """
    Return only the latest trading day's session.
    """
    df = df.copy()
    if df.empty:
        return df

    if not isinstance(df.index, pd.DatetimeIndex):
        df.index = pd.to_datetime(df.index)

    df = df.sort_index()
    if df.empty:
        return df

    today = df.index[-1].date()
    return df[df.index.date == today].copy()


# =========================
# Indicators
# =========================

def add_full_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """
    Indicators that use full history:
    - EMA9
    - EMA21
    - ATR14
    """
    df = df.copy()
    if df.empty:
        return df

    df["ema9"] = talib.EMA(df["close"], timeperiod=9)
    df["ema21"] = talib.EMA(df["close"], timeperiod=21)
    df["atr14"] = talib.ATR(df["high"], df["low"], df["close"], timeperiod=14)
    return df


def add_session_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """
    Session-only indicators:
    - VWAP
    - VWAP distance
    - session return
    - candle body ratio
    - wick ratio
    - ATR %
    """
    df = df.copy()
    if df.empty:
        return df

    tp = (df["high"] + df["low"] + df["close"]) / 3
    df["vwap"] = (tp * df["volume"]).cumsum() / df["volume"].cumsum()
    df["vwap_distance"] = ((df["close"] - df["vwap"]) / df["vwap"]) * 100

    session_open = df["open"].iloc[0]
    df["stock_return"] = ((df["close"] - session_open) / session_open) * 100

    df["body_ratio"] = (df["close"] - df["open"]).abs() / (df["high"] - df["low"]).replace(0, np.nan)

    upper_wick = df["high"] - df[["open", "close"]].max(axis=1)
    lower_wick = df[["open", "close"]].min(axis=1) - df["low"]
    df["wick_ratio"] = (upper_wick + lower_wick) / (df["high"] - df["low"]).replace(0, np.nan)

    if "atr14" in df.columns:
        df["atr_pct"] = (df["atr14"] / df["close"]) * 100
    else:
        df["atr_pct"] = np.nan

    return df


# =========================
# Gap
# =========================

def calculate_gap_pct(stock_full: pd.DataFrame) -> float:
    """
    Gap % = (today open - yesterday close) / yesterday close * 100
    """
    df = standardize_df(stock_full).copy()
    df = df.sort_index()

    if df.empty:
        return 0.0

    daily = df.groupby(df.index.date).agg(
        day_open=("open", "first"),
        day_close=("close", "last")
    )

    if len(daily) < 2:
        return 0.0

    today_open = daily.iloc[-1]["day_open"]
    prev_close = daily.iloc[-2]["day_close"]

    if pd.isna(today_open) or pd.isna(prev_close) or prev_close == 0:
        return 0.0

    return float(((today_open - prev_close) / prev_close) * 100)


# =========================
# Scoring helpers
# =========================

def score_rs_long(rs: float) -> int:
    if rs > 2.0:
        return 10
    elif rs > 1.0:
        return 8
    elif rs > 0.5:
        return 6
    elif rs > 0:
        return 4
    return 0


def score_rs_short(rs: float) -> int:
    weakness = -rs
    if weakness > 2.0:
        return 10
    elif weakness > 1.0:
        return 8
    elif weakness > 0.5:
        return 6
    elif weakness > 0:
        return 4
    return 0


def score_vwap_long(dist: float) -> int:
    if dist < -0.5:
        return 0
    elif dist < 0:
        return 2
    elif dist < 0.3:
        return 6
    elif dist < 1.2:
        return 10
    elif dist < 2.0:
        return 7
    elif dist < 3.0:
        return 4
    return 1


def score_vwap_short(dist: float) -> int:
    if dist > 0.5:
        return 0
    elif dist > 0:
        return 2
    elif dist > -0.3:
        return 6
    elif dist > -1.2:
        return 10
    elif dist > -2.0:
        return 7
    elif dist > -3.0:
        return 4
    return 1


def score_ema_long(df: pd.DataFrame) -> int:
    ema9 = df["ema9"].iloc[-1]
    ema21 = df["ema21"].iloc[-1]
    if pd.isna(ema9) or pd.isna(ema21):
        return 0
    if ema9 > ema21:
        return 10
    elif abs(ema9 - ema21) / ema21 < 0.002:
        return 4
    return 0


def score_ema_short(df: pd.DataFrame) -> int:
    ema9 = df["ema9"].iloc[-1]
    ema21 = df["ema21"].iloc[-1]
    if pd.isna(ema9) or pd.isna(ema21):
        return 0
    if ema9 < ema21:
        return 10
    elif abs(ema9 - ema21) / ema21 < 0.002:
        return 4
    return 0


def score_body(body_ratio: float) -> int:
    if pd.isna(body_ratio):
        return 0
    if body_ratio > 0.70:
        return 10
    elif body_ratio > 0.50:
        return 7
    elif body_ratio > 0.30:
        return 4
    return 0


def score_wick(wick_ratio: float) -> int:
    if pd.isna(wick_ratio):
        return 0
    if wick_ratio < 0.30:
        return 10
    elif wick_ratio < 0.50:
        return 6
    return 2


def trend_score_long(df: pd.DataFrame) -> float:
    ema = score_ema_long(df)
    body = score_body(df["body_ratio"].iloc[-1])
    wick = score_wick(df["wick_ratio"].iloc[-1])

    swing = 0
    if len(df) >= 6:
        lows = df["low"].tail(6).values
        swing_lows = [lows[1], lows[3], lows[5]]
        if swing_lows[0] < swing_lows[1] < swing_lows[2]:
            swing = 10
        elif swing_lows[1] > swing_lows[0] and swing_lows[2] > swing_lows[1]:
            swing = 7
        else:
            swing = 4

    return 0.35 * ema + 0.25 * swing + 0.25 * body + 0.15 * wick


def trend_score_short(df: pd.DataFrame) -> float:
    ema = score_ema_short(df)
    body = score_body(df["body_ratio"].iloc[-1])
    wick = score_wick(df["wick_ratio"].iloc[-1])

    swing = 0
    if len(df) >= 6:
        highs = df["high"].tail(6).values
        swing_highs = [highs[1], highs[3], highs[5]]
        if swing_highs[0] > swing_highs[1] > swing_highs[2]:
            swing = 10
        elif swing_highs[1] < swing_highs[0] and swing_highs[2] < swing_highs[1]:
            swing = 7
        else:
            swing = 4

    return 0.35 * ema + 0.25 * swing + 0.25 * body + 0.15 * wick


def score_volume(df: pd.DataFrame) -> int:
    """
    Simple fallback RVOL-like score using recent candles.
    """
    if len(df) < 5:
        return 0

    current_vol = df["volume"].iloc[-1]
    lookback = min(20, len(df) - 1)

    if lookback < 3:
        return 0

    avg_vol = df["volume"].tail(lookback + 1).iloc[:-1].mean()
    rvol = current_vol / avg_vol if avg_vol and avg_vol != 0 else 0

    if rvol > 2.5:
        return 10
    elif rvol > 1.5:
        return 8
    elif rvol > 1.2:
        return 6
    elif rvol > 0.8:
        return 4
    return 1


def score_volatility(atr_pct: float) -> int:
    if pd.isna(atr_pct):
        return 0
    if 1.0 <= atr_pct <= 3.0:
        return 10
    elif 0.7 <= atr_pct < 1.0:
        return 7
    elif 3.0 < atr_pct <= 4.5:
        return 6
    elif atr_pct < 0.7:
        return 3
    return 1


# =========================
# Session prep
# =========================

def prepare_stock_session(stock_full: pd.DataFrame):
    stock_full = standardize_df(stock_full)
    stock_full = add_full_indicators(stock_full).dropna().copy()

    stock_today = get_today_session(stock_full)
    stock_today = add_session_indicators(stock_today)

    return stock_full, stock_today


def prepare_nifty_session(nifty_full: pd.DataFrame):
    nifty_full = standardize_df(nifty_full)
    nifty_full = add_full_indicators(nifty_full).dropna().copy()

    nifty_today = get_today_session(nifty_full)
    nifty_today = add_session_indicators(nifty_today)

    nifty_today = nifty_today.rename(columns={
        "stock_return": "nifty_return",
        "vwap": "nifty_vwap",
        "vwap_distance": "nifty_vwap_distance",
        "body_ratio": "nifty_body_ratio",
        "wick_ratio": "nifty_wick_ratio",
        "atr_pct": "nifty_atr_pct"
    })

    return nifty_full, nifty_today


# =========================
# Ranking
# =========================

def rank_stock(stock_full: pd.DataFrame, nifty_full: pd.DataFrame):
    """
    Returns:
    {
        long_score,
        short_score,
        decision,
        rs_long,
        rs_short,
        vwap_long,
        vwap_short,
        trend_long,
        trend_short,
        volume_score,
        volatility_score,
        gap_pct
    }
    """
    if stock_full is None or nifty_full is None:
        return None

    _, stock_today = prepare_stock_session(stock_full)
    _, nifty_today = prepare_nifty_session(nifty_full)

    if stock_today.empty or nifty_today.empty:
        return None

    merged = stock_today.join(nifty_today[["nifty_return"]], how="inner")
    if merged.empty:
        return None

    latest = merged.iloc[-1]

    merged["rs"] = merged["stock_return"] - merged["nifty_return"]
    latest_rs = merged["rs"].iloc[-1]

    gap_pct = calculate_gap_pct(stock_full)

    rs_long = score_rs_long(latest_rs)
    rs_short = score_rs_short(latest_rs)

    vwap_long = score_vwap_long(latest["vwap_distance"])
    vwap_short = score_vwap_short(latest["vwap_distance"])

    trend_long = trend_score_long(merged)
    trend_short = trend_score_short(merged)

    vol_score = score_volume(merged)
    volat_score = score_volatility(latest["atr_pct"])

    gap_long = score_vwap_long(gap_pct)
    gap_short = score_vwap_short(gap_pct)

    long_score = (
        3.0 * rs_long +
        2.5 * vwap_long +
        2.0 * trend_long +
        1.5 * vol_score +
        0.5 * gap_long +
        0.5 * volat_score
    )

    short_score = (
        3.0 * rs_short +
        2.5 * vwap_short +
        2.0 * trend_short +
        1.5 * vol_score +
        0.5 * gap_short +
        0.5 * volat_score
    )

    if long_score >= 70 and long_score > short_score + 10:
        decision = "LONG"
    elif short_score >= 70 and short_score > long_score + 10:
        decision = "SHORT"
    else:
        decision = "SKIP"

    return {
        "long_score": round(float(long_score), 2),
        "short_score": round(float(short_score), 2),
        "decision": decision,
        "rs_long": rs_long,
        "rs_short": rs_short,
        "vwap_long": round(float(vwap_long), 2),
        "vwap_short": round(float(vwap_short), 2),
        "trend_long": round(float(trend_long), 2),
        "trend_short": round(float(trend_short), 2),
        "volume_score": vol_score,
        "volatility_score": volat_score,
        "gap_pct": round(float(gap_pct), 2),
    }


# =========================
# Live session prep
# =========================

def prepare_live_df(stock_full: pd.DataFrame) -> pd.DataFrame:
    """
    Full historical dataframe -> today's session only with live features.
    """
    df = standardize_df(stock_full).copy()
    df = add_full_indicators(df).dropna().copy()

    if df.empty:
        return df

    today_date = df.index[-1].date()
    today_df = df[df.index.date == today_date].copy()

    if today_df.empty:
        return today_df

    # Deduplicated logic: call add_session_indicators
    today_df = add_session_indicators(today_df)

    return today_df


def get_opening_range(df: pd.DataFrame, candles: int = 3):
    """
    First 3 x 5-min candles = first 15 minutes.
    Returns: OR high, OR low, OR mid
    """
    if df is None or len(df) < candles:
        return None, None, None

    or_df = df.head(candles)
    or_high = or_df["high"].max()
    or_low = or_df["low"].min()
    or_mid = (or_high + or_low) / 2
    return or_high, or_low, or_mid


def candle_volume_score(df: pd.DataFrame, lookback: int = 3) -> int:
    """
    Simple live volume expansion check.
    """
    if df is None or len(df) < lookback + 1:
        return 0

    current_vol = df["volume"].iloc[-1]
    avg_vol = df["volume"].tail(lookback + 1).iloc[:-1].mean()

    if avg_vol == 0 or pd.isna(avg_vol):
        return 0

    rvol = current_vol / avg_vol

    if rvol > 1.8:
        return 10
    elif rvol > 1.3:
        return 8
    elif rvol > 1.1:
        return 6
    elif rvol > 0.9:
        return 4
    return 1


# =========================
# Pullback confirmation
# =========================

def confirm_long_pullback(df: pd.DataFrame):
    """
    Long pullback:
    - trend bullish
    - above VWAP
    - reclaim VWAP
    - good body
    - clean wick
    - some volume support
    """
    if df is None or len(df) < 4:
        return False, "Not enough candles"

    latest = df.iloc[-1]
    prev = df.iloc[-2]

    cond_trend = latest["ema9"] > latest["ema21"]
    cond_vwap = latest["close"] > latest["vwap"]
    cond_reclaim = prev["close"] < prev["vwap"] and latest["close"] > latest["vwap"]
    cond_body = latest["body_ratio"] > 0.35
    cond_wick = latest["wick_ratio"] < 0.6
    cond_vol = candle_volume_score(df) >= 4

    score = sum([
        cond_trend,
        cond_vwap,
        cond_reclaim,
        cond_body,
        cond_wick,
        cond_vol
    ])

    if score >= 4:
        return True, "LONG pullback confirmed"
    return False, "LONG pullback not confirmed"


def confirm_short_pullback(df: pd.DataFrame):
    """
    Short pullback:
    - trend bearish
    - below VWAP
    - reject VWAP
    - good body
    - clean wick
    - some volume support
    """
    if df is None or len(df) < 4:
        return False, "Not enough candles"

    latest = df.iloc[-1]
    prev = df.iloc[-2]

    cond_trend = latest["ema9"] < latest["ema21"]
    cond_vwap = latest["close"] < latest["vwap"]
    cond_reject = prev["close"] > prev["vwap"] and latest["close"] < latest["vwap"]
    cond_body = latest["body_ratio"] > 0.35
    cond_wick = latest["wick_ratio"] < 0.6
    cond_vol = candle_volume_score(df) >= 4

    score = sum([
        cond_trend,
        cond_vwap,
        cond_reject,
        cond_body,
        cond_wick,
        cond_vol
    ])

    if score >= 4:
        return True, "SHORT pullback confirmed"
    return False, "SHORT pullback not confirmed"


# =========================
# Time filter
# =========================

def is_entry_time_allowed(df: pd.DataFrame) -> bool:
    """
    Live entry window:
    09:30 to 12:00 IST
    """
    if df is None or df.empty:
        return False

    latest_idx = df.index[-1]

    # handle timezone-aware / naive timestamps
    if hasattr(latest_idx, "tz_convert") and latest_idx.tz is not None:
        latest_time = latest_idx.tz_convert("Asia/Kolkata").time()
    else:
        latest_time = latest_idx.time()

    morning_start = time(9, 30)
    hard_stop = time(12, 0)

    return morning_start <= latest_time < hard_stop