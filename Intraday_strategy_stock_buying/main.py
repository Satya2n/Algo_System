# main.py
import os
import sys
import json
import time
import logging
import datetime
import config.settings as cfg
from config.credentials import (
    DHAN_CLIENT_CODE, DHAN_ACCESS_TOKEN, DHAN_PIN, DHAN_TOTP_SECRET,
    TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID,
)
from Dhan_Tradehull import Tradehull
from engine.state import StateManager
from engine.risk import RiskManager
from engine.execution import ExecutionEngine
from engine.strategy import (
    is_consolidating, is_breakout, is_pullback_entry_valid,
    standardize_df, get_today_session,
)

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

    # In-memory consolidation state — resets on restart (intraday only)
    # { sym: { state, direction, box_high, box_low, breakout_candle_ts } }
    consolidation_map = {}

    logger.info("Engine ready. Waiting for scanner to populate watchlist...")

    try:
        tsl.send_telegram_alert(
            message=(
                f"INTRADAY ENGINE STARTED\n"
                f"Mode: {'PAPER' if cfg.PAPER_TRADE else 'LIVE'}\n"
                f"Strategy: Consolidation Breakout (15-min score / 5-min entry)"
            ),
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
                        logger.warning(f"LTP failed — cooldown ({int(120-(now_ts-last_reconnect_ts))}s)")
                    else:
                        logger.warning("LTP fetch failed — reconnecting...")
                        try:
                            token_file = os.path.join("Dependencies", f"token_{datetime.date.today()}.txt")
                            if os.path.exists(token_file):
                                os.remove(token_file)
                            tsl = connect_tradehull()
                            executor.tsl = tsl
                            last_reconnect_ts = time.time()
                            logger.info("Reconnected.")
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
            # SLOW LOOP (60s): 3-State Consolidation Machine
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

            # ── Daily loss check (real LTP) ───────────────────
            unrealized_pnl = 0.0
            for t in active_trades:
                ltp = all_ltp.get(t.symbol, t.entry_price)
                unrealized_pnl += (ltp - t.entry_price) * t.qty if t.side == "BUY" \
                    else (t.entry_price - ltp) * t.qty
            total_pnl      = state_manager.state.realized_daily_pnl + unrealized_pnl
            max_loss       = -1.0 * (risk_manager.daily_starting_capital * cfg.MAX_DAILY_LOSS_PCT / 100.0)
            if total_pnl <= max_loss:
                logger.warning(f"MAX DAILY LOSS HIT ({total_pnl:.2f}). Halting entries.")
                continue

            # ── Entry window ──────────────────────────────────
            if current_time >= cfg.ENTRY_END:
                continue

            # ── Read dynamic watchlist (scanner writes this) ──
            watchlist = get_dynamic_watchlist()

            # Protect stocks in STATE 2/3 from scanner removal
            all_symbols = set(watchlist.keys()) | set(consolidation_map.keys())

            if not all_symbols:
                logger.info("Watchlist empty — scanner not yet run.")
                continue

            logger.info(
                f"Checking {len(all_symbols)} stocks | "
                f"Watching:{sum(1 for s in consolidation_map.values() if s['state']=='CONSOLIDATING')} consolidating | "
                f"Breakouts:{sum(1 for s in consolidation_map.values() if s['state']=='BREAKOUT')}"
            )

            for sym in all_symbols:
                if state_manager.get_trade(sym):
                    continue
                if state_manager.has_traded_today(sym):
                    continue

                # Direction from map (priority) or watchlist
                direction = (consolidation_map.get(sym, {}).get("direction")
                             or watchlist.get(sym, {}).get("direction"))
                if not direction:
                    continue

                stock_full = tsl.get_historical_data(tradingsymbol=sym, exchange="NSE", timeframe="5")
                if stock_full is None or len(stock_full) == 0:
                    continue

                current_state = consolidation_map.get(sym, {}).get("state", "WATCHING")

                # ─────────────────────────────────────────────
                # STATE 1 — WATCHING
                # ─────────────────────────────────────────────
                if current_state == "WATCHING":
                    consolidating, box_high, box_low, reason = is_consolidating(
                        stock_full,
                        direction=direction,
                        candles=cfg.CONSOLIDATION_CANDLES,
                        range_pct=cfg.CONSOLIDATION_RANGE_PCT,
                        atr_squeeze=cfg.ATR_SQUEEZE_RATIO,
                        vwap_pct=cfg.VWAP_PROXIMITY_PCT,
                    )
                    logger.info(
                        f"[{sym}] {direction} | Box:{box_low:.2f}-{box_high:.2f} | "
                        f"{'CONSOLIDATING ✓' if consolidating else reason}"
                    )
                    if consolidating:
                        consolidation_map[sym] = {
                            "state":             "CONSOLIDATING",
                            "direction":          direction,
                            "box_high":           box_high,
                            "box_low":            box_low,
                            "breakout_candle_ts": None,
                        }

                # ─────────────────────────────────────────────
                # STATE 2 — CONSOLIDATING (waiting for breakout)
                # ─────────────────────────────────────────────
                elif current_state == "CONSOLIDATING":
                    box_high = consolidation_map[sym]["box_high"]
                    box_low  = consolidation_map[sym]["box_low"]

                    broke_out, close, ts = is_breakout(stock_full, box_high, box_low, direction)

                    if broke_out:
                        logger.info(
                            f"[{sym}] BREAKOUT {direction} | "
                            f"Close:{close:.2f} | Box:{box_low:.2f}-{box_high:.2f}"
                        )
                        consolidation_map[sym]["state"]             = "BREAKOUT"
                        consolidation_map[sym]["breakout_candle_ts"] = ts

                    else:
                        # Invalidate if price broke WRONG direction beyond box
                        if direction == "LONG"  and close < box_low:
                            logger.info(f"[{sym}] Consolidation invalidated — price fell below box")
                            del consolidation_map[sym]
                        elif direction == "SHORT" and close > box_high:
                            logger.info(f"[{sym}] Consolidation invalidated — price rose above box")
                            del consolidation_map[sym]
                        else:
                            logger.info(f"[{sym}] Consolidating — waiting for breakout (close:{close:.2f})")

                # ─────────────────────────────────────────────
                # STATE 3 — BREAKOUT (waiting for pullback candle)
                # ─────────────────────────────────────────────
                elif current_state == "BREAKOUT":
                    box_high     = consolidation_map[sym]["box_high"]
                    box_low      = consolidation_map[sym]["box_low"]
                    breakout_ts  = consolidation_map[sym]["breakout_candle_ts"]

                    new_candle, valid, close, reason = is_pullback_entry_valid(
                        stock_full, box_high, box_low, direction,
                        breakout_ts=breakout_ts,
                        vwap_pct=cfg.VWAP_PULLBACK_PCT,
                    )

                    if not new_candle:
                        logger.info(f"[{sym}] Waiting for pullback candle...")
                        continue

                    if not valid:
                        logger.info(f"[{sym}] Pullback invalid — {reason} → reset to WATCHING")
                        del consolidation_map[sym]
                        continue

                    logger.info(
                        f"[{sym}] PULLBACK ENTRY {direction} | "
                        f"Close:{close:.2f} | Box:{box_low:.2f}-{box_high:.2f}"
                    )

                    # ── Calculate entry at box edge + 0.5% ───
                    tick = executor._get_tick_size(sym, close)
                    if direction == "LONG":
                        entry = executor._round_to_tick(box_high * (1 + cfg.LIMIT_BEYOND_BOX_PCT), tick)
                        sl    = executor._round_to_tick(box_low  * 0.999, tick)
                        side  = "BUY"
                    else:
                        entry = executor._round_to_tick(box_low  * (1 - cfg.LIMIT_BEYOND_BOX_PCT), tick)
                        sl    = executor._round_to_tick(box_high * 1.001, tick)
                        side  = "SELL"

                    risk = abs(entry - sl)
                    if risk <= 0:
                        del consolidation_map[sym]
                        continue

                    tp1 = (entry + cfg.TP1_REWARD_RATIO * risk) if direction == "LONG" \
                        else (entry - cfg.TP1_REWARD_RATIO * risk)

                    qty, _ = risk_manager.calculate_position_size(entry, sl)
                    if qty <= 0:
                        del consolidation_map[sym]
                        continue

                    trade_placed = executor.place_initial_orders(sym, side, qty, entry, sl, tp1)
                    del consolidation_map[sym]
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
