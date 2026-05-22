# scanner/nifty100_scanner_service.py
"""
Standalone NIFTY 100 Scanner Service.
Runs every 15 minutes, scores all NIFTY 100 stocks, writes dynamic_watchlist.json.
Start AFTER the main engine (reuses the same Dhan session token).
"""
import os
import sys
import time
import json
import logging
import datetime
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.credentials import DHAN_CLIENT_CODE, DHAN_ACCESS_TOKEN, DHAN_PIN, DHAN_TOTP_SECRET
from config.settings import (
    NIFTY100_SCAN_INTERVAL, SCORE_ADD_THRESHOLD, SCORE_REMOVE_THRESHOLD
)
from Dhan_Tradehull import Tradehull
from engine.strategy import rank_stock
from scanner.nifty100_stocks import NIFTY_100

Path("logs").mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    handlers=[
        logging.FileHandler("logs/scanner.log"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("SCANNER")

WATCHLIST_FILE = "state/dynamic_watchlist.json"
SCAN_START     = datetime.time(9, 0)
SCAN_END       = datetime.time(14, 30)


def connect() -> Tradehull:
    if DHAN_ACCESS_TOKEN:
        logger.info("Connecting via access token.")
        return Tradehull(DHAN_CLIENT_CODE, DHAN_ACCESS_TOKEN, mode="access_token")
    logger.info("Connecting via PIN + TOTP.")
    return Tradehull(DHAN_CLIENT_CODE, mode="pin_totp", pin=DHAN_PIN, totp_secret=DHAN_TOTP_SECRET)


def load_watchlist() -> dict:
    try:
        with open(WATCHLIST_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return {"watchlist": {}}


def save_watchlist(watchlist: dict):
    data = {
        "last_updated": datetime.datetime.now().isoformat(),
        "watchlist": watchlist,
    }
    tmp = WATCHLIST_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=4)
    os.replace(tmp, WATCHLIST_FILE)


def run_scan(tsl: Tradehull, existing: dict) -> dict:
    logger.info(f"Starting NIFTY 100 scan ({len(NIFTY_100)} stocks)...")

    nifty_full = tsl.get_historical_data("NIFTY", "INDEX", "5")
    if nifty_full is None or len(nifty_full) == 0:
        logger.error("NIFTY data unavailable — skipping scan, keeping existing watchlist.")
        return existing

    updated = dict(existing)

    for sym in NIFTY_100:
        try:
            df = tsl.get_historical_data(sym, "NSE", "5")
            if df is None or len(df) == 0:
                continue

            r = rank_stock(df, nifty_full)
            if not r:
                continue

            ls = r["long_score"]
            ss = r["short_score"]
            vol = r["volume_score"]

            # Determine best direction
            if ls >= SCORE_ADD_THRESHOLD and ls >= ss:
                direction, score = "LONG", ls
            elif ss >= SCORE_ADD_THRESHOLD and ss > ls:
                direction, score = "SHORT", ss
            else:
                # Not qualifying — check if should be removed
                if sym in updated:
                    cur_dir   = updated[sym]["direction"]
                    cur_score = ls if cur_dir == "LONG" else ss
                    if cur_score < SCORE_REMOVE_THRESHOLD:
                        logger.info(
                            f"[{sym}] REMOVED — score dropped to {cur_score:.0f} "
                            f"(threshold {SCORE_REMOVE_THRESHOLD})"
                        )
                        del updated[sym]
                continue

            if sym in updated:
                old_dir = updated[sym]["direction"]
                if old_dir != direction:
                    logger.info(f"[{sym}] DIRECTION CHANGED {old_dir} → {direction} | L:{ls:.0f} S:{ss:.0f}")
            else:
                logger.info(
                    f"[{sym}] ADDED — {direction} | Score:{score:.0f} | Vol:{vol}/10"
                )

            updated[sym] = {
                "direction": direction,
                "score":     round(score, 1),
                "vol":       vol,
                "rs_long":   r["rs_long"],
                "rs_short":  r["rs_short"],
            }

            time.sleep(0.25)

        except Exception as e:
            logger.warning(f"[{sym}] Error: {e}")

    n_long  = sum(1 for v in updated.values() if v["direction"] == "LONG")
    n_short = sum(1 for v in updated.values() if v["direction"] == "SHORT")
    logger.info(
        f"Scan complete — {len(updated)} stocks in watchlist | "
        f"LONG:{n_long} | SHORT:{n_short}"
    )
    return updated


def main():
    logger.info("NIFTY 100 Scanner Service started.")
    tsl = connect()

    while True:
        try:
            now = datetime.datetime.now().time()

            if SCAN_START <= now <= SCAN_END:
                data     = load_watchlist()
                existing = data.get("watchlist", {})
                updated  = run_scan(tsl, existing)
                save_watchlist(updated)
            else:
                logger.info("Outside scan window. Sleeping...")

            time.sleep(NIFTY100_SCAN_INTERVAL)

        except KeyboardInterrupt:
            logger.info("Scanner stopped.")
            break
        except Exception as e:
            logger.exception(f"Scanner loop error: {e}")
            time.sleep(60)


if __name__ == "__main__":
    main()
