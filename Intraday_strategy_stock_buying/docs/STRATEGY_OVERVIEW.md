# Intraday Equity Momentum & Pullback Strategy

## 1. Core Concept
This system trades **intraday momentum pullbacks** in the Indian Equity Market (NSE). It scans a predefined watchlist of highly liquid stocks and attempts to enter high-probability trades by combining Relative Strength, VWAP, and Candlestick structure.

## 2. Market Regime & Relative Strength
The algorithm doesn't trade in a vacuum. It mathematically compares every stock against the broader market index (`NIFTY`). 
- **Relative Strength (RS):** If NIFTY is up 0.5% but a stock is up 2.0%, the stock has a high RS score. The algorithm prefers going LONG on high RS stocks and SHORT on high weakness stocks.

## 3. The Setup (Entry Criteria)
The system requires a confluence of multiple signals to trigger an entry on the 5-minute timeframe:

### Long Setup:
1. **Trend Alignment:** The 9 EMA must be above the 21 EMA.
2. **VWAP Reclaim:** The previous candle must have closed *below* the VWAP, but the current candle must close *above* the VWAP, signaling institutional buyers defending the average price.
3. **Candle Quality:** 
   - The body of the candle must be solid (`body_ratio > 35%`).
   - The wick rejections must be clean.
4. **Volume:** The setup candle must show a Relative Volume (RVOL) expansion compared to the recent average.

### Short Setup:
1. **Trend Alignment:** The 9 EMA must be below the 21 EMA.
2. **VWAP Rejection:** The previous candle closed *above* VWAP, but the current candle closes *below* VWAP, signaling institutional sellers pushing the price down.
3. **Candle Quality:** Solid bearish body with clean wicks.
4. **Volume:** RVOL expansion.

## 4. Risk & Trade Management
- **Position Sizing:** Every trade is strictly capped at `1% Risk` of the total daily capital.
- **Entry Method:** Aggressive Limit Orders (padded by 1%) are used for entries to guarantee execution speed while capping "freak trade" slippage risk.
- **Stop Loss:** Market Stop Orders (`STOPMARKET`) are placed immediately. If the SL placement fails, the engine instantly flattens the position to prevent naked exposure (Orphan Protection).
- **Target 1 (TP1):** Set at a `1.5` Reward-to-Risk ratio. When hit, the system books 50% of the quantity via Limit Order and trails the remaining Stop Loss to breakeven.
- **Trailing Stop:** Uses a dynamic 15-minute trail (lowest low or highest high of the last 3 x 5-minute candles).

## 5. Capacity & Limits
- **Max Trades Per Day:** 5
- **Max Concurrent Open Trades:** 3
- **Max Daily Loss:** 2.0% of Starting Capital. If this threshold is breached, the engine halts new entries for the remainder of the session.
- **Time Limits:** Trading begins at `09:30 AM`. No entries after `12:00 PM`. All remaining open positions are force-closed via Market Order at `03:15 PM` (15:15).
