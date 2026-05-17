"""
Algo_DOS Offline Backtester
Copy and paste the sections below into your research_nifty.ipynb cells.
"""

# ==============================================================================
# CELL 1: SETUP AND IMPORTS
# ==============================================================================
import sys
import os
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
from datetime import datetime

# Add the parent directory to sys.path so we can import engine and config
sys.path.append(os.path.abspath(os.path.join(os.getcwd(), '..')))

from config.settings import (
    SYMBOL, EXCHANGE, TIMEFRAME, 
    SUPERTREND_ATR, SUPERTREND_MULTIPLIER, BREAKOUT_LOOKBACK,
    MAX_EXTENSION_ATR, MAX_CANDLE_RANGE_ATR, MIN_ATR,
    TRAIL_TO_BREAKEVEN_PCT
)
from engine.strategy import StrategyConfig, StrategyEngine
from Dhan_Tradehull import Tradehull
from config.credentials import DHAN_CLIENT_CODE, DHAN_ACCESS_TOKEN

# ==============================================================================
# CELL 2: FETCH HISTORICAL DATA
# ==============================================================================
print("Connecting to Tradehull...")
tsl = Tradehull(DHAN_CLIENT_CODE, DHAN_ACCESS_TOKEN, mode="access_token")

print("Fetching historical data...")
# Note: For deeper backtests, you may need a custom looping fetcher. 
# Tradehull usually limits to a few months at a time for 60-min data.
df = tsl.get_historical_data(SYMBOL, EXCHANGE, TIMEFRAME)

if df is not None and not df.empty:
    print(f"Successfully fetched {len(df)} rows.")
else:
    print("Failed to fetch historical data!")

# ==============================================================================
# CELL 3: RUN STRATEGY PIPELINE
# ==============================================================================
strategy_cfg = StrategyConfig(
    atr_period=SUPERTREND_ATR,
    atr_multiplier=SUPERTREND_MULTIPLIER,
    breakout_lookback=BREAKOUT_LOOKBACK,
    max_extension_atr=MAX_EXTENSION_ATR,
    max_candle_range_atr=MAX_CANDLE_RANGE_ATR,
    min_atr=MIN_ATR,
)
strategy = StrategyEngine(strategy_cfg)

print("Calculating Supertrend, Breakouts, and Signals...")
df = strategy.run_pipeline(df)
print("Pipeline complete!")

# Look at the signals generated
signals_only = df[df['signal'] != 'NO_TRADE']
print(f"Total Raw Signals Found: {len(signals_only)}")

# ==============================================================================
# CELL 4: SIMULATE THE TRADES (VECTORIZED SPREAD LOGIC)
# ==============================================================================
# We simulate a credit spread pricing model using the underlying spot movement.
# If spot moves 1%, spread decays/inflates by ~1.5x (Paper Trade Approx).
TARGET_PCT = 0.65
STOP_PCT = 0.85
TRAIL_PCT = TRAIL_TO_BREAKEVEN_PCT
ENTRY_CREDIT_MOCK = 45.0  # Assumed credit collected

trades = []
in_trade = False
current_trade = {}

for idx, row in df.iterrows():
    # 1. Check for Exits if we are in a trade
    if in_trade:
        # Calculate simulated credit based on spot movement
        movement_pct = (row['close'] - current_trade['entry_spot']) / current_trade['entry_spot']
        
        if current_trade['side'] == 'BULLISH_ENTRY':
            # Bull put spread benefits when spot rises
            simulated_credit = ENTRY_CREDIT_MOCK * (1.0 - 1.5 * movement_pct)
        else:
            # Bear call spread benefits when spot falls
            simulated_credit = ENTRY_CREDIT_MOCK * (1.0 + 1.5 * movement_pct)
            
        simulated_credit = max(simulated_credit, 0.01)

        # Check Trailing Stop Trigger
        if not current_trade['sl_moved']:
            if simulated_credit <= ENTRY_CREDIT_MOCK * (1.0 - TRAIL_PCT):
                current_trade['sl_moved'] = True
        
        # Determine active Stop Loss
        active_sl = ENTRY_CREDIT_MOCK if current_trade['sl_moved'] else ENTRY_CREDIT_MOCK * (1.0 + STOP_PCT)
        active_target = ENTRY_CREDIT_MOCK * (1.0 - TARGET_PCT)
        
        exit_reason = None
        
        # Did we hit Target or Stop?
        if simulated_credit <= active_target:
            exit_reason = "Target Hit"
            exit_price = active_target
        elif simulated_credit >= active_sl:
            exit_reason = "Trailing SL Hit" if current_trade['sl_moved'] else "Stop Loss Hit"
            exit_price = active_sl
            
        # Did the trend reverse?
        elif current_trade['side'] == 'BULLISH_ENTRY' and row['trend'] == 'bearish':
            exit_reason = "Trend Reversal"
            exit_price = simulated_credit
        elif current_trade['side'] == 'BEARISH_ENTRY' and row['trend'] == 'bullish':
            exit_reason = "Trend Reversal"
            exit_price = simulated_credit
            
        # Execute Exit
        if exit_reason:
            pnl = (ENTRY_CREDIT_MOCK - exit_price) * 50 # Assuming 1 lot of Nifty
            current_trade['exit_time'] = row['timestamp']
            current_trade['exit_spot'] = row['close']
            current_trade['exit_reason'] = exit_reason
            current_trade['pnl'] = round(pnl, 2)
            trades.append(current_trade)
            in_trade = False
            continue

    # 2. Check for Entries if we are NOT in a trade
    if not in_trade and row['signal'] != 'NO_TRADE':
        in_trade = True
        current_trade = {
            'entry_time': row['timestamp'],
            'entry_spot': row['close'],
            'side': row['signal'],
            'sl_moved': False,
        }

trade_df = pd.DataFrame(trades)
print(f"Total Trades Executed: {len(trade_df)}")

# ==============================================================================
# CELL 5: ANALYZE AND PLOT
# ==============================================================================
if not trade_df.empty:
    # Analytics
    trade_df['cumulative_pnl'] = trade_df['pnl'].cumsum()
    wins = trade_df[trade_df['pnl'] > 0]
    losses = trade_df[trade_df['pnl'] <= 0]
    
    win_rate = len(wins) / len(trade_df) * 100
    total_profit = trade_df['pnl'].sum()
    
    print(f"--- BACKTEST RESULTS ---")
    print(f"Total PnL: ₹{total_profit:.2f}")
    print(f"Win Rate: {win_rate:.2f}%")
    print(f"Avg Winning Trade: ₹{wins['pnl'].mean():.2f}" if not wins.empty else "No Wins")
    print(f"Avg Losing Trade: ₹{losses['pnl'].mean():.2f}" if not losses.empty else "No Losses")
    
    # Value Counts of Exit Reasons
    print("\nExit Reasons:")
    print(trade_df['exit_reason'].value_counts())

    # Plot Equity Curve
    plt.figure(figsize=(12, 6))
    plt.plot(trade_df.index, trade_df['cumulative_pnl'], marker='o', linestyle='-', color='b')
    plt.title("Algo_DOS Cumulative PnL (Credit Spread Approx)")
    plt.xlabel("Trade Number")
    plt.ylabel("Cumulative Profit (₹)")
    plt.grid(True)
    plt.axhline(0, color='r', linestyle='--')
    plt.show()
else:
    print("No trades were taken during this period.")
