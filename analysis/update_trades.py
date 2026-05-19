"""
Daily Trade Tracker
===================
Run this after market close each day to append the day's trades to Excel.

Usage:
    cd /Users/satya/Documents/Dev/Algo_setup
    python3 analysis/update_trades.py

Output:
    analysis/trades_data.xlsx  — cumulative Excel with all trade history
"""

import re
import sys
import os
from datetime import datetime, date, timedelta
from pathlib import Path

import pandas as pd

ROOT        = Path(__file__).resolve().parent.parent
EXCEL_PATH  = ROOT / "analysis" / "trades_data.xlsx"

INTRADAY_LOG = ROOT / "Intraday_strategy_stock_buying" / "logs" / "engine.log"
ALGODOS_LOG  = ROOT / "Algo_DOS" / "logs" / "engine.log"


# ══════════════════════════════════════════════════════════════════════════════
# PARSERS
# ══════════════════════════════════════════════════════════════════════════════

def parse_intraday_trades(log_path: Path, target_date: str) -> list:
    """Extract all trades for a specific date from Intraday engine log."""
    if not log_path.exists():
        print(f"Log not found: {log_path}")
        return []

    text = log_path.read_text(errors="ignore")
    lines = text.splitlines()

    entries = {}
    trades  = []

    entry_re = re.compile(
        r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}).*\[(\w+)\] Entry \| Side: (\w+) \| Qty: (\d+) \| Entry: ([\d.]+) \| SL: ([\d.]+) \| TP1: ([\d.]+)"
    )
    tp1_re = re.compile(
        r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}).*\[(\w+)\] LIVE TP1 HIT at ([\d.]+)"
    )
    sl_re = re.compile(
        r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}).*\[(\w+)\] LIVE SL HIT at ([\d.]+)"
    )

    seen = set()
    for line in lines:
        # Entry
        m = entry_re.search(line)
        if m:
            ts, sym, side, qty, entry, sl, tp1 = m.groups()
            if ts[:10] == target_date and (ts, sym) not in seen:
                seen.add((ts, sym))
                entries[sym] = {
                    "date": target_date,
                    "symbol": sym,
                    "side": side,
                    "entry_time": ts[11:16],
                    "entry_price": float(entry),
                    "stop_loss": float(sl),
                    "tp1": float(tp1),
                    "qty": int(qty),
                }

        # TP1 exit
        m = tp1_re.search(line)
        if m:
            ts, sym, exit_px = m.groups()
            if ts[:10] == target_date and sym in entries:
                t = entries.pop(sym)
                ep = float(exit_px)
                pnl = (ep - t["entry_price"]) * t["qty"] if t["side"] == "BUY" else (t["entry_price"] - ep) * t["qty"]
                entry_dt = datetime.strptime(f"{target_date} {t['entry_time']}", "%Y-%m-%d %H:%M")
                exit_dt  = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")
                duration = int((exit_dt - entry_dt).total_seconds() / 60)
                trades.append({**t,
                    "exit_time": ts[11:16], "exit_price": ep,
                    "exit_type": "TP1", "pnl": round(pnl, 2),
                    "duration_mins": duration,
                    "result": "WIN" if pnl > 0 else "LOSS"
                })

        # SL exit
        m = sl_re.search(line)
        if m:
            ts, sym, exit_px = m.groups()
            if ts[:10] == target_date and sym in entries:
                t = entries.pop(sym)
                ep = float(exit_px)
                pnl = (ep - t["entry_price"]) * t["qty"] if t["side"] == "BUY" else (t["entry_price"] - ep) * t["qty"]
                entry_dt = datetime.strptime(f"{target_date} {t['entry_time']}", "%Y-%m-%d %H:%M")
                exit_dt  = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")
                duration = int((exit_dt - entry_dt).total_seconds() / 60)
                trades.append({**t,
                    "exit_time": ts[11:16], "exit_price": ep,
                    "exit_type": "SL", "pnl": round(pnl, 2),
                    "duration_mins": duration,
                    "result": "WIN" if pnl > 0 else "LOSS"
                })

    return trades


def parse_algodos_trades(log_path: Path, target_date: str) -> list:
    """Extract Algo_DOS paper trades for a specific date."""
    if not log_path.exists():
        return []

    text = log_path.read_text(errors="ignore")
    lines = text.splitlines()
    trades = []
    active = None

    entry_re = re.compile(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}).*PAPER ENTRY.*Signal: (\w+).*Credit: ([\d.]+)")
    exit_re  = re.compile(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}).*PAPER EXIT.*Reason: (.+)\n.*PnL: ([+-]?[\d.]+)")

    # Simple approach — look for PAPER ENTRY/EXIT in log
    entry_re2 = re.compile(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}).*\[PAPER ENTRY\] (\w+) \| .* qty=(\d+) \| credit=([\d.]+)")
    exit_re2  = re.compile(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}).*\[PAPER EXIT\] (.+) \| pnl=([+-]?[\d.]+)")

    for line in lines:
        m = entry_re2.search(line)
        if m and m.group(1)[:10] == target_date:
            ts, signal, qty, credit = m.groups()
            active = {
                "date": target_date,
                "signal": signal,
                "entry_time": ts[11:16],
                "entry_credit": float(credit),
                "qty": int(qty),
            }

        m = exit_re2.search(line)
        if m and m.group(1)[:10] == target_date and active:
            ts, reason, pnl = m.groups()
            active.update({
                "exit_time": ts[11:16],
                "exit_reason": reason.strip(),
                "pnl": float(pnl),
                "result": "WIN" if float(pnl) > 0 else "LOSS"
            })
            trades.append(active)
            active = None

    return trades


# ══════════════════════════════════════════════════════════════════════════════
# DAILY SUMMARY
# ══════════════════════════════════════════════════════════════════════════════

def make_daily_summary(trades: list, target_date: str) -> dict:
    if not trades:
        return {
            "date": target_date, "total_trades": 0,
            "wins": 0, "losses": 0, "win_rate_pct": 0,
            "total_pnl": 0, "best_trade": 0, "worst_trade": 0,
            "avg_duration_mins": 0, "notes": ""
        }
    pnls = [t["pnl"] for t in trades]
    wins = [p for p in pnls if p > 0]
    loss = [p for p in pnls if p <= 0]
    durations = [t.get("duration_mins", 0) for t in trades]
    return {
        "date": target_date,
        "total_trades": len(trades),
        "wins": len(wins),
        "losses": len(loss),
        "win_rate_pct": round(len(wins) / len(trades) * 100, 1),
        "total_pnl": round(sum(pnls), 2),
        "best_trade": round(max(pnls), 2),
        "worst_trade": round(min(pnls), 2),
        "avg_duration_mins": round(sum(durations) / len(durations), 1) if durations else 0,
        "notes": ""
    }


# ══════════════════════════════════════════════════════════════════════════════
# EXCEL WRITER
# ══════════════════════════════════════════════════════════════════════════════

INTRADAY_COLS = [
    "date", "symbol", "side", "entry_time", "exit_time", "duration_mins",
    "entry_price", "exit_price", "stop_loss", "tp1",
    "exit_type", "qty", "pnl", "result"
]

ALGODOS_COLS = [
    "date", "signal", "entry_time", "exit_time",
    "entry_credit", "qty", "pnl", "exit_reason", "result"
]

SUMMARY_COLS = [
    "date", "total_trades", "wins", "losses", "win_rate_pct",
    "total_pnl", "best_trade", "worst_trade", "avg_duration_mins", "notes"
]


def load_sheet(excel_path: Path, sheet: str, cols: list) -> pd.DataFrame:
    if excel_path.exists():
        try:
            return pd.read_excel(excel_path, sheet_name=sheet)
        except Exception:
            pass
    return pd.DataFrame(columns=cols)


def save_excel(intraday_df, algodos_df, summary_df):
    with pd.ExcelWriter(EXCEL_PATH, engine="openpyxl") as writer:
        intraday_df.to_excel(writer, sheet_name="Intraday_Trades", index=False)
        algodos_df.to_excel(writer, sheet_name="Algo_DOS_Trades", index=False)
        summary_df.to_excel(writer, sheet_name="Daily_Summary", index=False)

        # Auto-width columns
        for sheet_name in writer.sheets:
            ws = writer.sheets[sheet_name]
            for col in ws.columns:
                max_len = max((len(str(c.value)) for c in col if c.value), default=10)
                ws.column_dimensions[col[0].column_letter].width = min(max_len + 2, 25)


# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════

def run(target_date: str = None):
    if not target_date:
        target_date = str(date.today())

    print(f"\nUpdating trade data for {target_date}...")

    # Load existing Excel sheets
    intraday_df = load_sheet(EXCEL_PATH, "Intraday_Trades", INTRADAY_COLS)
    algodos_df  = load_sheet(EXCEL_PATH, "Algo_DOS_Trades",  ALGODOS_COLS)
    summary_df  = load_sheet(EXCEL_PATH, "Daily_Summary",    SUMMARY_COLS)

    # Remove existing entries for this date (allow re-run)
    if "date" in intraday_df.columns:
        intraday_df = intraday_df[intraday_df["date"].astype(str) != target_date]
    if "date" in algodos_df.columns:
        algodos_df  = algodos_df[algodos_df["date"].astype(str) != target_date]
    if "date" in summary_df.columns:
        summary_df  = summary_df[summary_df["date"].astype(str) != target_date]

    # Parse today's trades
    intraday_trades = parse_intraday_trades(INTRADAY_LOG, target_date)
    algodos_trades  = parse_algodos_trades(ALGODOS_LOG,  target_date)

    print(f"  Intraday trades found : {len(intraday_trades)}")
    print(f"  Algo_DOS trades found : {len(algodos_trades)}")

    # Append new rows
    if intraday_trades:
        new_rows = pd.DataFrame(intraday_trades)[INTRADAY_COLS]
        intraday_df = pd.concat([intraday_df, new_rows], ignore_index=True)

    if algodos_trades:
        new_rows = pd.DataFrame(algodos_trades)[[c for c in ALGODOS_COLS if c in pd.DataFrame(algodos_trades).columns]]
        algodos_df = pd.concat([algodos_df, new_rows], ignore_index=True)

    # Daily summary
    summary_row = make_daily_summary(intraday_trades, target_date)
    summary_df  = pd.concat([summary_df, pd.DataFrame([summary_row])], ignore_index=True)
    summary_df  = summary_df.sort_values("date")

    # Save
    save_excel(intraday_df, algodos_df, summary_df)

    print(f"  Excel saved → {EXCEL_PATH}")
    print(f"  Intraday PnL : ₹{summary_row['total_pnl']:+.2f}")
    print(f"  Win rate     : {summary_row['win_rate_pct']}%")
    print(f"  Trades       : {summary_row['wins']}W / {summary_row['losses']}L")


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else None
    run(target)
