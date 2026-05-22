"""
NIFTY 100 Scanner — run locally to see scores for all NIFTY 100 stocks.
Usage: python nifty100_scanner.py
Shows LONG and SHORT scores sorted by best opportunity.
"""

import sys
import time
import pandas as pd

sys.path.insert(0, ".")

from config.credentials import DHAN_CLIENT_CODE, DHAN_ACCESS_TOKEN, DHAN_PIN, DHAN_TOTP_SECRET
from Dhan_Tradehull import Tradehull
from engine.strategy import rank_stock

NIFTY_100 = [
    "RELIANCE", "TCS", "HDFCBANK", "BHARTIARTL", "ICICIBANK",
    "INFY", "SBIN", "HINDUNILVR", "ITC", "LT",
    "KOTAKBANK", "AXISBANK", "BAJFINANCE", "MARUTI", "HCLTECH",
    "SUNPHARMA", "TITAN", "WIPRO", "ULTRACEMCO", "ASIANPAINT",
    "BAJAJFINSV", "NTPC", "POWERGRID", "TATAMOTORS", "ADANIPORTS",
    "ONGC", "COALINDIA", "TATASTEEL", "JSWSTEEL", "INDUSINDBK",
    "GRASIM", "DRREDDY", "TECHM", "CIPLA", "DIVISLAB",
    "ADANIENT", "BRITANNIA", "HINDALCO", "BAJAJ-AUTO", "HEROMOTOCO",
    "EICHERMOT", "TATACONSUM", "NESTLEIND", "BPCL", "IOC",
    "APOLLOHOSP", "HDFCLIFE", "SBILIFE", "UPL", "M&M",
    "LTIM", "VEDL", "PIDILITIND", "DABUR", "MARICO",
    "SIEMENS", "HAVELLS", "BERGEPAINT", "GODREJCP", "MUTHOOTFIN",
    "CHOLAFIN", "MOTHERSON", "DLF", "NAUKRI", "ZOMATO",
    "PAYTM", "POLYCAB", "TRENT", "DMART", "COLPAL",
    "TORNTPHARM", "AUROPHARMA", "LUPIN", "BIOCON", "ALKEM",
    "IDFCFIRSTB", "FEDERALBNK", "BANDHANBNK", "CANBK", "PNB",
    "SAIL", "NMDC", "CONCOR", "GAIL", "PETRONET",
    "MFSL", "COFORGE", "PERSISTENT", "MPHASIS", "LTTS",
    "PIIND", "APLAPOLLO", "NAVINFLUOR", "SOLARINDS", "SKFINDIA",
    "TATACHEM", "JUBLFOOD", "PATANJALI", "ESCORTS", "JKCEMENT",
]


def connect():
    if DHAN_ACCESS_TOKEN:
        return Tradehull(DHAN_CLIENT_CODE, DHAN_ACCESS_TOKEN, mode="access_token")
    return Tradehull(DHAN_CLIENT_CODE, mode="pin_totp", pin=DHAN_PIN, totp_secret=DHAN_TOTP_SECRET)


def main():
    print("Connecting to Dhan...")
    tsl = connect()

    print("Fetching NIFTY data...")
    nifty_full = tsl.get_historical_data(tradingsymbol="NIFTY", exchange="INDEX", timeframe="5")
    if nifty_full is None or len(nifty_full) == 0:
        print("ERROR: Could not fetch NIFTY data.")
        return

    results = []
    total = len(NIFTY_100)

    for i, sym in enumerate(NIFTY_100, 1):
        print(f"[{i:3}/{total}] {sym:<15}", end="", flush=True)
        try:
            df = tsl.get_historical_data(tradingsymbol=sym, exchange="NSE", timeframe="5")
            if df is None or len(df) == 0:
                print("  no data")
                continue

            r = rank_stock(df, nifty_full)
            if not r:
                print("  rank failed")
                continue

            results.append({
                "symbol":   sym,
                "long":     r["long_score"],
                "short":    r["short_score"],
                "decision": r["decision"],
                "vol":      r["volume_score"],
                "rs_l":     r["rs_long"],
                "rs_s":     r["rs_short"],
                "vwap_l":   r["vwap_long"],
                "vwap_s":   r["vwap_short"],
                "trend_l":  r["trend_long"],
                "trend_s":  r["trend_short"],
                "gap":      r["gap_pct"],
            })
            print(f"  L:{r['long_score']:5.1f}  S:{r['short_score']:5.1f}  {r['decision']}")

        except Exception as e:
            print(f"  error: {e}")

        time.sleep(0.3)

    if not results:
        print("No results.")
        return

    df_res = pd.DataFrame(results)

    print("\n" + "="*80)
    print("TOP LONG SETUPS (score >= 60)")
    print("="*80)
    longs = df_res[df_res["long"] >= 60].sort_values("long", ascending=False)
    if longs.empty:
        print("  None found")
    else:
        for _, row in longs.iterrows():
            flag = " *** SIGNAL ***" if row["long"] >= 72 else ""
            print(
                f"  {row['symbol']:<15} L:{row['long']:5.1f} | "
                f"Vol:{row['vol']}/10 | RS:{row['rs_l']} | "
                f"VWAP:{row['vwap_l']:.0f} | Trend:{row['trend_l']:.1f} | "
                f"Gap:{row['gap']:+.2f}%{flag}"
            )

    print("\n" + "="*80)
    print("TOP SHORT SETUPS (score >= 60)")
    print("="*80)
    shorts = df_res[df_res["short"] >= 60].sort_values("short", ascending=False)
    if shorts.empty:
        print("  None found")
    else:
        for _, row in shorts.iterrows():
            flag = " *** SIGNAL ***" if row["short"] >= 72 else ""
            print(
                f"  {row['symbol']:<15} S:{row['short']:5.1f} | "
                f"Vol:{row['vol']}/10 | RS:{row['rs_s']} | "
                f"VWAP:{row['vwap_s']:.0f} | Trend:{row['trend_s']:.1f} | "
                f"Gap:{row['gap']:+.2f}%{flag}"
            )

    print("\n" + "="*80)
    print(f"Scanned {len(results)}/{total} stocks. Decisions: "
          f"LONG={len(df_res[df_res['decision']=='LONG'])} "
          f"SHORT={len(df_res[df_res['decision']=='SHORT'])} "
          f"SKIP={len(df_res[df_res['decision']=='SKIP'])}")
    print("="*80)


if __name__ == "__main__":
    main()
