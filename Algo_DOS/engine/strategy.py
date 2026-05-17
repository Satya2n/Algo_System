from __future__ import annotations

from dataclasses import dataclass
import numpy as np
import pandas as pd
import talib


@dataclass
class StrategyConfig:
    atr_period: int = 10
    atr_multiplier: float = 3.0
    breakout_lookback: int = 5
    max_extension_atr: float = 0.35
    max_candle_range_atr: float = 1.5
    min_atr: float = 50.0
    allow_reentry_same_trend: bool = False


class StrategyEngine:
    def __init__(self, cfg: StrategyConfig):
        self.cfg = cfg

    def add_supertrend(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()

        high = df["high"].astype(float).values
        low = df["low"].astype(float).values
        close = df["close"].astype(float).values

        atr = talib.ATR(high, low, close, timeperiod=self.cfg.atr_period)

        hl2 = (high + low) / 2.0
        upper_basic = hl2 + self.cfg.atr_multiplier * atr
        lower_basic = hl2 - self.cfg.atr_multiplier * atr

        final_upper = np.full(len(df), np.nan)
        final_lower = np.full(len(df), np.nan)
        supertrend = np.full(len(df), np.nan)
        direction = np.full(len(df), 0)

        for i in range(len(df)):
            if np.isnan(atr[i]):
                continue

            if i == 0 or np.isnan(final_upper[i - 1]):
                final_upper[i] = upper_basic[i]
                final_lower[i] = lower_basic[i]
                supertrend[i] = upper_basic[i]
                direction[i] = -1
                continue

            final_upper[i] = upper_basic[i] if (upper_basic[i] < final_upper[i - 1]) or (close[i - 1] > final_upper[i - 1]) else final_upper[i - 1]
            final_lower[i] = lower_basic[i] if (lower_basic[i] > final_lower[i - 1]) or (close[i - 1] < final_lower[i - 1]) else final_lower[i - 1]

            if supertrend[i - 1] == final_upper[i - 1]:
                if close[i] <= final_upper[i]:
                    supertrend[i] = final_upper[i]
                    direction[i] = -1
                else:
                    supertrend[i] = final_lower[i]
                    direction[i] = 1
            else:
                if close[i] >= final_lower[i]:
                    supertrend[i] = final_lower[i]
                    direction[i] = 1
                else:
                    supertrend[i] = final_upper[i]
                    direction[i] = -1

        df["atr"] = atr
        df["supertrend"] = supertrend
        df["trend_dir"] = direction
        df["trend"] = np.where(df["trend_dir"] == 1, "bullish", "bearish")
        return df

    def add_breakout_features(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        lb = self.cfg.breakout_lookback

        df["prev_high"] = df["high"].shift(1).rolling(lb).max()
        df["prev_low"] = df["low"].shift(1).rolling(lb).min()

        df["bull_breakout"] = df["close"] > df["prev_high"]
        df["bear_breakdown"] = df["close"] < df["prev_low"]
        return df

    def add_entry_events(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        df["bull_entry_event"] = df["bull_breakout"] & (~df["bull_breakout"].shift(1).fillna(False))
        df["bear_entry_event"] = df["bear_breakdown"] & (~df["bear_breakdown"].shift(1).fillna(False))
        return df

    def add_overextension_filters(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()

        df["candle_range"] = df["high"] - df["low"]
        df["range_atr"] = df["candle_range"] / df["atr"]

        df["bull_extension_atr"] = (df["close"] - df["prev_high"]) / df["atr"]
        df["bear_extension_atr"] = (df["prev_low"] - df["close"]) / df["atr"]

        df["bull_ok"] = (
            df["bull_entry_event"]
            & (df["atr"] >= self.cfg.min_atr)
            & (df["bull_extension_atr"] <= self.cfg.max_extension_atr)
            & (df["range_atr"] <= self.cfg.max_candle_range_atr)
        )

        df["bear_ok"] = (
            df["bear_entry_event"]
            & (df["atr"] >= self.cfg.min_atr)
            & (df["bear_extension_atr"] <= self.cfg.max_extension_atr)
            & (df["range_atr"] <= self.cfg.max_candle_range_atr)
        )

        return df

    def generate_signals(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        df["signal"] = "NO_TRADE"

        bullish_condition = (df["trend"] == "bullish") & (df["bull_ok"])
        bearish_condition = (df["trend"] == "bearish") & (df["bear_ok"])

        df.loc[bullish_condition, "signal"] = "BULLISH_ENTRY"
        df.loc[bearish_condition, "signal"] = "BEARISH_ENTRY"

        return df

    def run_pipeline(self, df: pd.DataFrame) -> pd.DataFrame:
        df = self.add_supertrend(df)
        df = self.add_breakout_features(df)
        df = self.add_entry_events(df)
        df = self.add_overextension_filters(df)
        df = self.generate_signals(df)
        return df
