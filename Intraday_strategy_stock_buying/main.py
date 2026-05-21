# main.py
import os
import sys
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
from engine.strategy import prepare_live_df, is_entry_time_allowed, rank_stock, confirm_long_pullback, confirm_short_pullback

os.makedirs("logs", exist_ok=True)

# Logging setup
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    handlers=[
        logging.FileHandler("logs/engine.log"),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger("MAIN")

def connect_tradehull() -> Tradehull:
    if DHAN_ACCESS_TOKEN:
        logger.info("Logging in via access token.")
        return Tradehull(DHAN_CLIENT_CODE, DHAN_ACCESS_TOKEN, mode="access_token")
    elif DHAN_PIN and DHAN_TOTP_SECRET:
        logger.info("Logging in via PIN + TOTP.")
        return Tradehull(DHAN_CLIENT_CODE, mode="pin_totp", pin=DHAN_PIN, totp_secret=DHAN_TOTP_SECRET)
    else:
        raise ValueError("No valid credentials. Set DHAN_ACCESS_TOKEN or DHAN_PIN + DHAN_TOTP_SECRET in credentials.py")


def get_watchlist():
    try:
        return pd.read_csv("data/preferred_watchlist.csv")["symbol"].dropna().tolist()
    except Exception:
        logger.error("Could not read preferred_watchlist.csv")
        return []

def main():
    logger.info("Initializing Intraday Pullback Engine...")
    
    # 1. Connect
    tsl = connect_tradehull()
    
    # 2. Modules
    state_manager = StateManager()
    risk_manager = RiskManager(cfg)
    executor = ExecutionEngine(tsl, state_manager)
    watchlist = get_watchlist()

    logger.info(f"Loaded {len(watchlist)} symbols from watchlist.")

    try:
        tsl.send_telegram_alert(
            message=f"🚀 INTRADAY ENGINE STARTED\nMode: {'PAPER' if cfg.PAPER_TRADE else 'LIVE'}\nSymbols loaded: {len(watchlist)}",
            receiver_chat_id=TELEGRAM_CHAT_ID,
            bot_token=TELEGRAM_BOT_TOKEN
        )
    except Exception as e:
        logger.error(f"Failed to send startup telegram alert: {e}")

    last_slow_poll    = 0
    last_reconnect_ts = 0   # track last reconnect to enforce 2-min cooldown

    while True:
        try:
            now = datetime.datetime.now()
            current_time = now.time()

            # Market Hours Check
            if current_time < cfg.ENTRY_START or current_time >= cfg.FORCE_EXIT:
                if current_time >= cfg.FORCE_EXIT and state_manager.get_active_trade_count() > 0:
                    logger.warning("Force exit time reached. Squaring off all trades.")
                    executor.square_off_all()
                logger.info("Outside market hours. Sleeping...")
                time.sleep(60)
                continue

            # Lock Daily Capital (Prevents shrinking sizing bug)
            if risk_manager.daily_starting_capital == 0.0:
                risk_manager.set_daily_capital(cfg.CAPITAL)
                logger.info(f"Capital locked at ₹{cfg.CAPITAL:,.0f}")

            current_ts = time.time()
            
            # ==========================================
            # FAST LOOP (5 seconds): Manage Open Trades
            # ==========================================
            active_trades = list(state_manager.state.active_trades.values())
            if active_trades:
                all_ltp = tsl.get_ltp_data(names=[t.symbol for t in active_trades])
                if not all_ltp:
                    now_ts = time.time()
                    if now_ts - last_reconnect_ts < 120:
                        logger.warning(f"LTP failed — reconnect cooldown ({int(120-(now_ts-last_reconnect_ts))}s left)")
                    else:
                        logger.warning("LTP fetch failed — attempting reconnect...")
                        try:
                            import os
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
                    if not ltp: continue

                    updated_trade, closed = executor.manage_open_trade(trade, ltp)
                    if closed:
                        state_manager.remove_trade(trade.symbol)

                time.sleep(cfg.FAST_POLL_INTERVAL_SECONDS)

            # ==========================================
            # SLOW LOOP (60 seconds): Find New Setups
            # ==========================================
            if current_ts - last_slow_poll >= cfg.POLL_INTERVAL_SECONDS:
                last_slow_poll = current_ts
                
                # Check constraints
                if state_manager.state.trade_count_today >= cfg.MAX_TRADES_PER_DAY:
                    continue
                if state_manager.get_active_trade_count() >= cfg.MAX_CONCURRENT_TRADES:
                    continue

                # Max Daily Loss Enforcement
                unrealized_pnl = sum([t.pnl for t in active_trades])
                total_daily_pnl = state_manager.state.realized_daily_pnl + unrealized_pnl
                max_loss_amount = -1.0 * (risk_manager.daily_starting_capital * cfg.MAX_DAILY_LOSS_PCT / 100.0)
                if total_daily_pnl <= max_loss_amount:
                    logger.warning(f"MAX DAILY LOSS HIT ({total_daily_pnl:.2f} <= {max_loss_amount:.2f}). Halting new entries.")
                    continue

                logger.info("Running slow poll for setups...")
                nifty_full = tsl.get_historical_data(tradingsymbol="NIFTY", exchange="INDEX", timeframe="5")
                if nifty_full is None or len(nifty_full) == 0:
                    now_ts = time.time()
                    if now_ts - last_reconnect_ts < 120:
                        logger.warning(f"NIFTY empty — reconnect cooldown ({int(120-(now_ts-last_reconnect_ts))}s left)")
                    else:
                        logger.warning("NIFTY data empty — attempting reconnect...")
                        try:
                            import os
                            token_file = os.path.join("Dependencies", f"token_{datetime.date.today()}.txt")
                            if os.path.exists(token_file):
                                os.remove(token_file)
                            tsl = connect_tradehull()
                            executor.tsl = tsl
                            last_reconnect_ts = time.time()
                            logger.info("Reconnected successfully.")
                        except Exception as re:
                            logger.error(f"Reconnect failed: {re}")
                    continue

                for sym in watchlist:
                    if state_manager.get_trade(sym):
                        continue  # already in active trade
                    if state_manager.has_traded_today(sym):
                        continue  # already traded this symbol today — no re-entry

                    stock_full = tsl.get_historical_data(tradingsymbol=sym, exchange="NSE", timeframe="5")
                    if stock_full is None or len(stock_full) == 0:
                        continue

                    live_df = prepare_live_df(stock_full)
                    if not is_entry_time_allowed(live_df):
                        continue

                    # Evaluate Logic
                    rank_result = rank_stock(stock_full, nifty_full)
                    if not rank_result: continue
                    decision = rank_result.get("decision", "SKIP")

                    # Volume filter — skip if < 4/10 (RVOL < 0.8x, no conviction)
                    if rank_result.get("volume_score", 0) < 4:
                        logger.info(f"[{sym}] Skipped — low volume ({rank_result['volume_score']}/10)")
                        continue
                    
                    signal = "NO_TRADE"
                    if decision == "LONG":
                        ok, _ = confirm_long_pullback(live_df)
                        if ok: signal = "ENTER_LONG"
                    elif decision == "SHORT":
                        ok, _ = confirm_short_pullback(live_df)
                        if ok: signal = "ENTER_SHORT"

                    if signal != "NO_TRADE":
                        score = rank_result.get("long_score") if decision == "LONG" else rank_result.get("short_score")
                        logger.info(
                            f"[{sym}] SIGNAL {signal} | "
                            f"Score:{score:.1f} | Vol:{rank_result.get('volume_score')}/10 | "
                            f"RS:{rank_result.get('rs_long' if decision=='LONG' else 'rs_short'):.1f} | "
                            f"VWAP:{rank_result.get('vwap_long' if decision=='LONG' else 'vwap_short'):.1f} | "
                            f"Trend:{rank_result.get('trend_long' if decision=='LONG' else 'trend_short'):.1f} | "
                            f"Gap:{rank_result.get('gap_pct',0):+.2f}%"
                        )

                    if signal != "NO_TRADE":
                        latest = live_df.iloc[-1]
                        entry = latest["close"]
                        
                        if signal == "ENTER_LONG":
                            sl = latest["low"] * 0.999
                            risk = entry - sl
                            # L-6: minimum SL floor — 0.3% of entry to avoid noise hits
                            min_risk = entry * 0.003
                            if risk < min_risk:
                                sl   = entry - min_risk
                                risk = min_risk
                            tp1 = entry + (cfg.TP1_REWARD_RATIO * risk)
                            side = "BUY"
                        else:
                            sl = latest["high"] * 1.001
                            risk = sl - entry
                            # L-6: minimum SL floor — 0.3% of entry to avoid noise hits
                            min_risk = entry * 0.003
                            if risk < min_risk:
                                sl   = entry + min_risk
                                risk = min_risk
                            tp1 = entry - (cfg.TP1_REWARD_RATIO * risk)
                            side = "SELL"

                        if risk <= 0: continue

                        qty, risk_amount = risk_manager.calculate_position_size(entry, sl)

                        if qty > 0:
                            # L-7: Liquidity hard block if impact > 5%
                            try:
                                avg_bar_value = (live_df["close"] * live_df["volume"]).mean()
                                position_value = qty * entry
                                impact_pct = (position_value / avg_bar_value) * 100 if avg_bar_value > 0 else 0
                                if impact_pct > 5.0:
                                    logger.warning(
                                        f"[{sym}] SKIPPED — liquidity impact {impact_pct:.1f}% > 5% "
                                        f"(position ₹{position_value:,.0f} vs avg bar ₹{avg_bar_value:,.0f})"
                                    )
                                    continue
                                elif impact_pct > 2.0:
                                    logger.warning(
                                        f"[{sym}] LOW LIQUIDITY WARNING: {impact_pct:.1f}% impact"
                                    )
                            except Exception:
                                pass

                            executor.place_initial_orders(sym, side, qty, entry, sl, tp1)
                            # Break out to avoid firing 3 trades in one 5-minute bar
                            break

            if not active_trades:
                time.sleep(cfg.POLL_INTERVAL_SECONDS)

        except KeyboardInterrupt:
            logger.info("Engine gracefully stopped by user.")
            break
        except Exception as e:
            logger.exception(f"Main loop error: {e}")
            time.sleep(10)

if __name__ == "__main__":
    main()
