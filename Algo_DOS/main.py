"""
Main engine entrypoint.

Flow:
1) load state
2) connect Tradehull
3) fetch historical data
4) compute signals
5) execute paper/live
6) save state after every step
"""

from __future__ import annotations

import logging
from pathlib import Path

from config.credentials import (
    DHAN_CLIENT_CODE,
    DHAN_ACCESS_TOKEN,
    DHAN_PIN,
    DHAN_TOTP_SECRET,
)
from config.settings import (
    SYMBOL,
    EXCHANGE,
    TIMEFRAME,
    PAPER_TRADE,
    CAPITAL,
    RISK_PER_TRADE_PCT,
    MAX_TRADES_PER_DAY,
    MAX_MARGIN_UTILIZATION_PCT,
    BUFFER_MARGIN_PCT,
    MAX_SPREAD_WIDTH,
    MIN_CREDIT,
    SUPERTREND_ATR,
    SUPERTREND_MULTIPLIER,
    BREAKOUT_LOOKBACK,
    MAX_EXTENSION_ATR,
    MAX_CANDLE_RANGE_ATR,
    MIN_ATR,
    ESTIMATED_MARGIN_PER_LOT,
    POLL_INTERVAL_SECONDS,
    FAST_POLL_INTERVAL_SECONDS,
    TRAIL_TO_BREAKEVEN_PCT,
)
from engine.state import StateStore
from engine.risk import RiskConfig, RiskManager
from engine.strategy import StrategyConfig, StrategyEngine
from engine.execution import ExecutionConfig, ExecutionEngine
from engine.telegram_control import TelegramConfig, TelegramNotifier
from Dhan_Tradehull import Tradehull


# =========================================================
# LOGGER
# =========================================================

def setup_logger() -> logging.Logger:
    Path("logs").mkdir(exist_ok=True)

    logger = logging.getLogger("algo_dos")
    logger.setLevel(logging.INFO)

    if not logger.handlers:
        fmt = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s")

        fh = logging.FileHandler("logs/engine.log")
        fh.setFormatter(fmt)

        sh = logging.StreamHandler()
        sh.setFormatter(fmt)

        logger.addHandler(fh)
        logger.addHandler(sh)

    return logger


# =========================================================
# BROKER CONNECTION
# =========================================================

def connect_tradehull(logger: logging.Logger) -> Tradehull:
    """
    Connect using access token only.
    Tradehull will load instrument_df after successful login.
    """
    try:
        if DHAN_ACCESS_TOKEN:
            tsl = Tradehull(
                DHAN_CLIENT_CODE,
                DHAN_ACCESS_TOKEN,
                mode="access_token",
            )
        elif DHAN_PIN and DHAN_TOTP_SECRET:
            tsl = Tradehull(
                DHAN_CLIENT_CODE,
                mode="pin_totp",
                pin=DHAN_PIN,
                totp_secret=DHAN_TOTP_SECRET,
            )
        else:
            raise ValueError("No valid Dhan credentials found (token or TOTP).")

        if not hasattr(tsl, "instrument_df"):
            raise RuntimeError("Tradehull login failed. instrument_df missing.")

        logger.info("Tradehull connected successfully")
        return tsl

    except Exception as e:
        logger.exception(f"Broker login failed: {e}")
        raise


# =========================================================
# MAIN
# =========================================================

def main():
    logger = setup_logger()
    logger.info("Engine starting")

    # -------------------------------------------------
    # LOAD ENGINE STATE
    # -------------------------------------------------
    store = StateStore("state/engine_state.json")
    state = store.load()
    state = store.reset_daily_counters_if_new_day(state)
    store.save(state)

    # -------------------------------------------------
    # RISK MANAGER
    # -------------------------------------------------
    risk_cfg = RiskConfig(
        capital=CAPITAL,
        risk_per_trade_pct=RISK_PER_TRADE_PCT,
        max_trades_per_day=MAX_TRADES_PER_DAY,
        max_margin_utilization_pct=MAX_MARGIN_UTILIZATION_PCT,
        buffer_margin_pct=BUFFER_MARGIN_PCT,
        max_spread_width=MAX_SPREAD_WIDTH,
        min_credit=MIN_CREDIT,
        margin_per_lot=ESTIMATED_MARGIN_PER_LOT,
    )
    risk = RiskManager(risk_cfg)
    logger.info("Risk manager initialized")

    # -------------------------------------------------
    # CONNECT BROKER
    # -------------------------------------------------
    tsl = connect_tradehull(logger)

    # -------------------------------------------------
    # FETCH HISTORICAL DATA
    # -------------------------------------------------
    nifty_df = tsl.get_historical_data(
        SYMBOL,
        EXCHANGE,
        TIMEFRAME,
    )

    if nifty_df is None or nifty_df.empty:
        raise RuntimeError("Historical data fetch failed.")

    logger.info(f"{SYMBOL} rows fetched: {len(nifty_df)}")

    # -------------------------------------------------
    # STRATEGY CONFIG
    # -------------------------------------------------
    strategy_cfg = StrategyConfig(
        atr_period=SUPERTREND_ATR,
        atr_multiplier=SUPERTREND_MULTIPLIER,
        breakout_lookback=BREAKOUT_LOOKBACK,
        max_extension_atr=MAX_EXTENSION_ATR,
        max_candle_range_atr=MAX_CANDLE_RANGE_ATR,
        min_atr=MIN_ATR,
    )
    strategy = StrategyEngine(strategy_cfg)

    # -------------------------------------------------
    # EXECUTION ENGINE CONFIG
    # -------------------------------------------------
    exec_cfg = ExecutionConfig(
        symbol=SYMBOL,
        exchange=EXCHANGE,
        paper_trade=PAPER_TRADE,
        trail_to_breakeven_pct=TRAIL_TO_BREAKEVEN_PCT,
    )

    executor = ExecutionEngine(
        tsl=tsl,
        risk=risk,
        state_store=store,
        cfg=exec_cfg,
        logger=logger,
        state=state,
    )

    telegram = TelegramNotifier(
        TelegramConfig(),
        logger=logger,
    )

    telegram.send(
        f"✅ Engine started in CONTINUOUS loop\n"
        f"Mode: {'PAPER' if PAPER_TRADE else 'LIVE'}\n"
        f"Symbol: {SYMBOL}\n"
        f"Timeframe: {TIMEFRAME}"
    )

    import time
    from datetime import datetime, time as datetime_time
    last_slow_poll = 0
    last_sleep_log = 0
    
    logger.info("Entering continuous execution loop. Press Ctrl+C to stop.")

    try:
        while True:
            now_ts = time.time()
            now_time = datetime.now().time()
            
            # -------------------------------------------------
            # MARKET HOURS SLEEPER
            # -------------------------------------------------
            market_start = datetime_time(9, 15)
            market_end = datetime_time(15, 30)
            
            if not (market_start <= now_time <= market_end):
                if now_ts - last_sleep_log >= 3600:  # Log once per hour
                    logger.info(f"Outside market hours ({market_start.strftime('%H:%M')} - {market_end.strftime('%H:%M')}). Engine is hibernating...")
                    last_sleep_log = now_ts
                time.sleep(60)
                continue
            
            # -------------------------------------------------
            # FAST LOOP: Check exit targets/stop-losses
            # -------------------------------------------------
            if executor.state.trade_mode == "IN_TRADE":
                # We do not pass signal_row here so it fetches live LTP if not paper_trade
                res = executor.manage_open_trade()
                if res and res.ok:
                    logger.info(res.message)

            # -------------------------------------------------
            # SLOW LOOP: Fetch data and look for entry
            # -------------------------------------------------
            if now_ts - last_slow_poll >= POLL_INTERVAL_SECONDS:
                logger.info("Running slow poll for historical data & new setups...")

                # Fetch new historical data
                nifty_df = tsl.get_historical_data(
                    SYMBOL,
                    EXCHANGE,
                    TIMEFRAME,
                )

                if nifty_df is None or nifty_df.empty:
                    # Check for auth failure — attempt reconnect
                    logger.warning("NIFTY data fetch returned empty. Attempting reconnect...")
                    try:
                        tsl = connect_tradehull(logger)
                        telegram.send("⚠️ Algo_DOS: NIFTY data fetch failed. Reconnected to Dhan.")
                        logger.info("Reconnected successfully.")
                    except Exception as reconnect_err:
                        logger.error(f"Reconnect failed: {reconnect_err}")
                        telegram.send(f"❌ Algo_DOS: Reconnect FAILED. Engine not scanning!\nError: {reconnect_err}")
                else:
                    # Run Strategy Pipeline
                    nifty_df = strategy.run_pipeline(nifty_df)

                    # If we aren't in a trade, evaluate the latest candle for entry
                    if executor.state.trade_mode != "IN_TRADE":
                        latest_row = nifty_df.iloc[-1]
                        result = executor.on_signal(latest_row)
                        if result and result.ok:
                            logger.info(result.message)

                last_slow_poll = time.time()

            time.sleep(FAST_POLL_INTERVAL_SECONDS)

    except KeyboardInterrupt:
        logger.info("Engine gracefully stopped by user.")
    finally:
        store.save(executor.state)
        logger.info("Engine shutdown complete.")


# =========================================================
# ENTRYPOINT
# =========================================================

if __name__ == "__main__":
    main()