# main.py
import os
import sys
import json
import time
import logging
import datetime
import pandas as pd
import config.settings as cfg
from config.credentials import (
    DHAN_CLIENT_CODE, DHAN_ACCESS_TOKEN, DHAN_PIN, DHAN_TOTP_SECRET,
    TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID,
)
from Dhan_Tradehull import Tradehull
from engine.state import StateManager
from engine.risk import RiskManager
from engine.execution import ExecutionEngine
from engine.strategy import is_consolidating, is_breakout

os.makedirs("logs", exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    handlers=[
        logging.FileHandler("logs/engine.log"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("MAIN")

WATCHLIST_FILE = "state/dynamic_watchlist.json"


def connect_tradehull() -> Tradehull:
    if DHAN_ACCESS_TOKEN:
        logger.info("Logging in via access token.")
        return Tradehull(DHAN_CLIENT_CODE, DHAN_ACCESS_TOKEN, mode="access_token")
    elif DHAN_PIN and DHAN_TOTP_SECRET:
        logger.info("Logging in via PIN + TOTP.")
        return Tradehull(DHAN_CLIENT_CODE, mode="pin_totp", pin=DHAN_PIN, totp_secret=DHAN_TOTP_SECRET)
    else:
        raise ValueError("No valid credentials configured.")


def get_dynamic_watchlist() -> dict:
    """Read dynamic watchlist written by the scanner service."""
    try:
        with open(WATCHLIST_FILE, "r") as f:
            data = json.load(f)
        return data.get("watchlist", {})
    except Exception:
        return {}


def main():
    logger.info("Initializing Intraday Consolidation Breakout Engine...")

    tsl           = connect_tradehull()
    state_manager = StateManager()
    risk_manager  = RiskManager(cfg)
    executor      = ExecutionEngine(tsl, state_manager)

    logger.info("Engine ready. Waiting for scanner to populate watchlist...")

    try:
        tsl.send_telegram_alert(
            message=f"INTRADAY ENGINE STARTED\nMode: {'PAPER' if cfg.PAPER_TRADE else 'LIVE'}\nStrategy: Consolidation Breakout",
            receiver_chat_id=TELEGRAM_CHAT_ID,
            bot_token=TELEGRAM_BOT_TOKEN,
        )
    except Exception as e:
        logger.error(f"Startup Telegram failed: {e}")

    last_slow_poll      = 0
    last_reconnect_ts   = 0
    last_error_alert_ts = 0

    while True:
        try:
            now          = datetime.datetime.now()
            current_time = now.time()

            # ── Market hours gate ──────────────────────────────
            if current_time < cfg.ENTRY_START or current_time >= cfg.FORCE_EXIT:
                if current_time >= cfg.FORCE_EXIT and state_manager.get_active_trade_count() > 0:
                    logger.warning("Force exit time. Squaring off all trades.")
                    executor.square_off_all()
                logger.info("Outside market hours. Sleeping...")
                time.sleep(60)
                continue

            # ── Lock daily capital ─────────────────────────────
            if risk_manager.daily_starting_capital == 0.0:
                risk_manager.set_daily_capital(cfg.CAPITAL)
                logger.info(f"Capital locked at Rs{cfg.CAPITAL:,.0f}")

            current_ts = time.time()

            # ══════════════════════════════════════════════════
            # FAST LOOP (5s): Manage open trades — SL / TP1
            # ══════════════════════════════════════════════════
            active_trades = list(state_manager.state.active_trades.values())
            all_ltp = {}
            if active_trades:
                all_ltp = tsl.get_ltp_data(names=[t.symbol for t in active_trades])
                if not all_ltp:
                    now_ts = time.time()
                    if now_ts - last_reconnect_ts < 120:
                        logger.warning(f"LTP failed — reconnect cooldown ({int(120-(now_ts-last_reconnect_ts))}s left)")
                    else:
                        logger.warning("LTP fetch failed — attempting reconnect...")
                        try:
                            token_file = os.path.join("Dependencies", f"token_{datetime.date.today()}.txt")
                            if os.path.exists(token_file):
                                os.remove(token_file)
                            tsl = connect_tradehull()
                            executor.tsl = tsl
                            last_reconnect_ts = time.time()
                            logger.info("Reconnected successfully.")
                        except Exception as re:
                            logger.error(f"Reconnect failed: {re}")
                    time.sleep(cfg.FAST_POLL_INTERVAL_SECONDS)
                    continue

                for trade in active_trades:
                    ltp = all_ltp.get(trade.symbol)
                    if not ltp:
                        continue
                    _, closed = executor.manage_open_trade(trade, ltp)
                    if closed:
                        state_manager.remove_trade(trade.symbol)

                time.sleep(cfg.FAST_POLL_INTERVAL_SECONDS)

            # ══════════════════════════════════════════════════
            # SLOW LOOP (60s): Consolidation + Breakout Check
            # ══════════════════════════════════════════════════
            if current_ts - last_slow_poll < cfg.POLL_INTERVAL_SECONDS:
                if not active_trades:
                    time.sleep(cfg.POLL_INTERVAL_SECONDS)
                continue

            last_slow_poll = current_ts

            # ── Constraints ───────────────────────────────────
            if state_manager.state.trade_count_today >= cfg.MAX_TRADES_PER_DAY:
                continue
            if state_manager.get_active_trade_count() >= cfg.MAX_CONCURRENT_TRADES:
                continue

            # ── Daily loss enforcement ────────────────────────
            unrealized_pnl = 0.0
            for t in active_trades:
                ltp = all_ltp.get(t.symbol, t.entry_price)
                unrealized_pnl += (ltp - t.entry_price) * t.qty if t.side == "BUY" \
                    else (t.entry_price - ltp) * t.qty
            total_daily_pnl = state_manager.state.realized_daily_pnl + unrealized_pnl
            max_loss_amount = -1.0 * (risk_manager.daily_starting_capital * cfg.MAX_DAILY_LOSS_PCT / 100.0)
            if total_daily_pnl <= max_loss_amount:
                logger.warning(f"MAX DAILY LOSS HIT ({total_daily_pnl:.2f}). Halting entries.")
                continue

            # ── Entry window ──────────────────────────────────
            if current_time >= cfg.ENTRY_END:
                continue

            # ── Read dynamic watchlist from scanner ───────────
            watchlist = get_dynamic_watchlist()
            if not watchlist:
                logger.info("Dynamic watchlist empty — scanner not yet run or no qualifying stocks.")
                continue

            logger.info(f"Checking {len(watchlist)} stocks for consolidation breakout...")

            # ── Consolidation + Breakout scan ─────────────────
            for sym, info in watchlist.items():
                direction = info["direction"]  # "LONG" or "SHORT"

                if state_manager.get_trade(sym):
                    continue
                if state_manager.has_traded_today(sym):
                    continue

                stock_full = tsl.get_historical_data(tradingsymbol=sym, exchange="NSE", timeframe="5")
                if stock_full is None or len(stock_full) == 0:
                    continue

                # Check consolidation
                consolidating, box_high, box_low, reason = is_consolidating(
                    stock_full,
                    candles=cfg.CONSOLIDATION_CANDLES,
                    range_pct=cfg.CONSOLIDATION_RANGE_PCT,
                    atr_squeeze=cfg.ATR_SQUEEZE_RATIO,
                    vwap_pct=cfg.VWAP_PROXIMITY_PCT,
                )

                logger.info(
                    f"[{sym}] {direction} | Box:{box_low:.2f}-{box_high:.2f} | "
                    f"{'CONSOLIDATING' if consolidating else reason}"
                )

                if not consolidating:
                    continue

                # Check breakout
                broke_out, close_price = is_breakout(stock_full, box_high, box_low, direction)
                if not broke_out:
                    logger.info(f"[{sym}] Consolidating but no breakout yet (close:{close_price:.2f})")
                    continue

                logger.info(f"[{sym}] BREAKOUT {direction} | Close:{close_price:.2f} | Box:{box_low:.2f}-{box_high:.2f}")

                # ── Calculate entry, SL, TP1 from box levels ──
                tick = executor._get_tick_size(sym, close_price)

                if direction == "LONG":
                    entry = executor._round_to_tick(close_price * (1 + cfg.LIMIT_BEYOND_BOX_PCT), tick)
                    sl    = executor._round_to_tick(box_low * 0.999, tick)
                    side  = "BUY"
                else:
                    entry = executor._round_to_tick(close_price * (1 - cfg.LIMIT_BEYOND_BOX_PCT), tick)
                    sl    = executor._round_to_tick(box_high * 1.001, tick)
                    side  = "SELL"

                risk = abs(entry - sl)
                if risk <= 0:
                    continue

                tp1 = entry + (cfg.TP1_REWARD_RATIO * risk) if direction == "LONG" \
                    else entry - (cfg.TP1_REWARD_RATIO * risk)

                qty, _ = risk_manager.calculate_position_size(entry, sl)
                if qty <= 0:
                    continue

                trade_placed = executor.place_initial_orders(sym, side, qty, entry, sl, tp1)
                if trade_placed:
                    break  # one entry per slow poll cycle

        except KeyboardInterrupt:
            logger.info("Engine stopped by user.")
            break
        except Exception as e:
            logger.exception(f"Main loop error: {e}")
            now_ts = time.time()
            if now_ts - last_error_alert_ts > 300:
                try:
                    import requests
                    from urllib.parse import quote
                    msg = (
                        f"ENGINE ERROR\n"
                        f"Error : {str(e)[:200]}\n"
                        f"Open trades : {state_manager.get_active_trade_count()}\n"
                        f"Retrying in 10s."
                    )
                    url = (f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
                           f"/sendMessage?chat_id={TELEGRAM_CHAT_ID}&text={quote(msg)}")
                    requests.get(url, timeout=10)
                    last_error_alert_ts = now_ts
                except Exception:
                    pass
            time.sleep(10)


if __name__ == "__main__":
    main()
