# engine/execution.py
import logging
import time as pytime
from typing import Optional, Tuple
from .state import ActiveTrade, StateManager
import config.settings as cfg


class ExecutionEngine:
    def __init__(self, tsl, state_manager: StateManager):
        self.tsl = tsl
        self.state_manager = state_manager
        self.logger = logging.getLogger("ExecutionEngine")

    def _mock_order_id(self, prefix: str) -> str:
        return f"{prefix}_{int(pytime.time() * 1000)}"

    def _send_alert(self, title: str, trade: ActiveTrade, slippage_pts: float = 0.0, slippage_pct: float = 0.0):
        from config.credentials import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
        try:
            is_entry = "ENTRY" in title
            is_sl    = "SL" in title
            is_tp    = "TP1" in title
            is_sq    = "SQUARE" in title

            if is_entry:
                direction = "LONG (BUY)" if trade.side == "BUY" else "SHORT (SELL)"
                slip_flag = f"⚠️ High" if abs(slippage_pct) > 0.3 else "✅ Normal"
                msg = (
                    f"🚀 ENTRY — {trade.symbol}\n"
                    f"──────────────────\n"
                    f"Direction : {direction}\n"
                    f"Qty       : {trade.qty}\n"
                    f"Entry     : ₹{trade.entry_price:.2f}\n"
                    f"Stop Loss : ₹{trade.stop_loss:.2f}\n"
                    f"Target    : ₹{trade.tp1:.2f}\n"
                    f"Risk/Reward: 1:1.75\n"
                    f"Slippage  : {slippage_pts:+.2f} pts ({slippage_pct:+.3f}%) {slip_flag}"
                )
            elif is_tp:
                msg = (
                    f"✅ TARGET HIT — {trade.symbol}\n"
                    f"──────────────────\n"
                    f"Qty   : {trade.qty}\n"
                    f"Entry : ₹{trade.entry_price:.2f}\n"
                    f"Target: ₹{trade.tp1:.2f}\n"
                    f"PnL   : ₹{trade.pnl:.2f}"
                )
            elif is_sl:
                msg = (
                    f"❌ STOP LOSS HIT — {trade.symbol}\n"
                    f"──────────────────\n"
                    f"Qty   : {trade.qty}\n"
                    f"Entry : ₹{trade.entry_price:.2f}\n"
                    f"SL    : ₹{trade.stop_loss:.2f}\n"
                    f"PnL   : ₹{trade.pnl:.2f}"
                )
            elif is_sq:
                msg = (
                    f"⏹ FORCE EXIT — {trade.symbol}\n"
                    f"──────────────────\n"
                    f"Qty : {trade.qty}\n"
                    f"PnL : ₹{trade.pnl:.2f}"
                )
            else:
                msg = f"[{title}] {trade.symbol}"

            self.tsl.send_telegram_alert(
                message=msg,
                receiver_chat_id=TELEGRAM_CHAT_ID,
                bot_token=TELEGRAM_BOT_TOKEN
            )
        except Exception:
            pass

    def _send_raw_alert(self, message: str):
        """Send alert via direct HTTP — does not depend on Dhan session."""
        from config.credentials import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
        try:
            import requests
            from urllib.parse import quote
            url = (f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
                   f"/sendMessage?chat_id={TELEGRAM_CHAT_ID}&text={quote(message)}")
            requests.get(url, timeout=10)
        except Exception as e:
            self.logger.warning(f"Telegram alert failed: {e}")

    def _reconnect(self):
        """Attempt fresh TOTP login and refresh tsl connection."""
        try:
            from config.credentials import (
                DHAN_CLIENT_CODE, DHAN_ACCESS_TOKEN, DHAN_PIN, DHAN_TOTP_SECRET
            )
            import os
            from datetime import date as _date
            token_file = os.path.join("Dependencies", f"token_{_date.today()}.txt")
            if os.path.exists(token_file):
                os.remove(token_file)
            from Dhan_Tradehull import Tradehull
            if DHAN_ACCESS_TOKEN:
                self.tsl = Tradehull(DHAN_CLIENT_CODE, DHAN_ACCESS_TOKEN, mode="access_token")
            else:
                self.tsl = Tradehull(DHAN_CLIENT_CODE, mode="pin_totp",
                                     pin=DHAN_PIN, totp_secret=DHAN_TOTP_SECRET)
            self.logger.info("ExecutionEngine reconnected to Dhan.")
            return True
        except Exception as e:
            self.logger.error(f"Reconnect failed: {e}")
            return False

    def _place_order(self, symbol, qty, price, trigger_price, order_type, txn_type):
        """Single order placement call."""
        return self.tsl.order_placement(
            tradingsymbol=symbol, exchange="NSE", quantity=qty,
            price=price, trigger_price=trigger_price,
            order_type=order_type, transaction_type=txn_type, trade_type="MIS"
        )

    def _live_market_exit(self, trade: ActiveTrade, ltp: float, reason: str) -> float:
        """
        Exit with 3 attempts:
          1. MARKET order
          2. MARKET order after reconnect (2s wait)
          3. LIMIT order at 1% favorable price (3s wait)
        If all 3 fail → urgent Telegram alert to close manually.
        """
        exit_txn  = "SELL" if trade.side == "BUY" else "BUY"
        tick      = self._get_tick_size(trade.symbol)

        attempts = [
            ("MARKET", 0,     0),
            ("MARKET", 0,     0),     # retry after reconnect
            ("LIMIT",  None,  0),     # limit with buffer — set below
        ]

        for i, (order_type, price, _) in enumerate(attempts, 1):
            # Set limit price for attempt 3
            if order_type == "LIMIT":
                if exit_txn == "SELL":
                    price = self._round_to_tick(ltp * 0.99, tick)  # below market
                else:
                    price = self._round_to_tick(ltp * 1.01, tick)  # above market

            try:
                self.logger.info(f"[{trade.symbol}] Exit attempt {i}/3 | {order_type} | price={price or 'MKT'}")
                order_id = self._place_order(
                    trade.symbol, trade.qty, price or 0, 0, order_type, exit_txn
                )
                if order_id:
                    exit_price = self.tsl.get_executed_price(orderid=order_id)
                    if exit_price:
                        self.logger.info(f"[{trade.symbol}] Exit filled at ₹{exit_price} (attempt {i})")
                        return float(exit_price)

            except Exception as e:
                self.logger.error(f"[{trade.symbol}] Exit attempt {i} failed: {e}")

            # Between attempts
            if i == 1:
                pytime.sleep(2)
                self._reconnect()
            elif i == 2:
                pytime.sleep(3)

        # All 3 failed
        self.logger.critical(f"[{trade.symbol}] ALL EXIT ATTEMPTS FAILED. MANUAL ACTION REQUIRED!")
        self._send_raw_alert(
            f"🚨 URGENT — EXIT FAILED 3 TIMES\n"
            f"Stock    : {trade.symbol}\n"
            f"Reason   : {reason}\n"
            f"Side     : {trade.side} | Qty: {trade.qty}\n"
            f"Action   : CLOSE MANUALLY IN DHAN APP NOW!"
        )
        return ltp

    # =========================================================
    # TICK SIZE HELPER
    # =========================================================

    def _get_tick_size(self, symbol: str) -> float:
        """
        Fetch tick size from Dhan instrument file.
        SEM_TICK_SIZE is stored in PAISE — divide by 100 to get rupees.
        Example: SEM_TICK_SIZE=5 → 5 paise → ₹0.05 (standard NSE equity tick)
        """
        try:
            inst = getattr(self.tsl, "instrument_df", None)
            if inst is not None and not inst.empty:
                row = inst[inst["SEM_TRADING_SYMBOL"] == symbol]
                if not row.empty:
                    tick_paise = float(row.iloc[0].get("SEM_TICK_SIZE", 5.0))
                    tick_rs = tick_paise / 100.0  # paise → rupees
                    if tick_rs > 0:
                        return tick_rs
        except Exception:
            pass
        return 0.05

    def _round_to_tick(self, price: float, tick: float) -> float:
        """Round price to nearest valid tick size."""
        return round(round(price / tick) * tick, 4)

    # =========================================================
    # ENTRY
    # =========================================================

    def place_initial_orders(
        self,
        symbol: str,
        side: str,
        qty: int,
        entry_price: float,
        sl_price: float,
        tp1: float
    ) -> Optional[ActiveTrade]:

        if qty <= 0:
            return None

        self.logger.info(
            f"[{symbol}] Entry | Side: {side} | Qty: {qty} | "
            f"Entry: {entry_price} | SL: {sl_price} | TP1: {tp1}"
        )

        # ── PAPER ─────────────────────────────────────────────
        if cfg.PAPER_TRADE:
            trade = ActiveTrade(
                symbol=symbol,
                side=side,
                qty=qty,
                entry_price=entry_price,
                entry_order_id=self._mock_order_id("PAPER_ENT"),
                sl_order_id="PAPER_SL",
                stop_loss=sl_price,
                tp1=tp1,
                remaining_qty=qty,
                trail_sl=sl_price
            )
            self.state_manager.add_trade(trade)
            self._send_alert("PAPER ENTRY FILLED", trade)
            return trade

        # ── LIVE ──────────────────────────────────────────────
        entry_txn = "BUY" if side == "BUY" else "SELL"
        tick      = self._get_tick_size(symbol)
        raw_limit = entry_price * 1.01 if side == "BUY" else entry_price * 0.99
        limit_price = self._round_to_tick(raw_limit, tick)
        self.logger.info(f"[{symbol}] Tick size: ₹{tick} | Limit price: ₹{limit_price}")

        # ── Entry with retry ──────────────────────────────────
        # Attempt 1: LIMIT order
        # Attempt 2: LIMIT order after reconnect
        # Attempt 3: MARKET order (guaranteed fill if market open)
        entry_orderid    = None
        actual_entry_price = None

        entry_attempts = [
            ("LIMIT",  limit_price),
            ("LIMIT",  limit_price),   # after reconnect
            ("MARKET", 0),             # fallback to market
        ]

        for attempt_num, (order_type, price) in enumerate(entry_attempts, 1):
            try:
                self.logger.info(f"[{symbol}] Entry attempt {attempt_num}/3 | {order_type} @ ₹{price or 'MKT'}")
                entry_orderid = self._place_order(symbol, qty, price, 0, order_type, entry_txn)

                if not entry_orderid:
                    raise ValueError("No order ID returned")

                pytime.sleep(1.5)
                order_status = self.tsl.get_order_status(orderid=entry_orderid)
                if str(order_status).upper() in {"REJECTED", "CANCELLED", "FAILED", "INVALID"}:
                    raise ValueError(f"Order status: {order_status}")

                actual_entry_price = self.tsl.get_executed_price(orderid=entry_orderid)
                if actual_entry_price:
                    break  # success

            except Exception as e:
                self.logger.warning(f"[{symbol}] Entry attempt {attempt_num} failed: {e}")
                entry_orderid = None
                actual_entry_price = None

                if attempt_num == 1:
                    pytime.sleep(2)
                    self._reconnect()
                elif attempt_num == 2:
                    pytime.sleep(2)

        if not actual_entry_price:
            self.logger.error(f"[{symbol}] All 3 entry attempts failed. Trade skipped.")
            self._send_raw_alert(
                f"❌ ENTRY FAILED — {symbol}\n"
                f"All 3 attempts failed (LIMIT×2 + MARKET)\n"
                f"Trade NOT placed. Check Dhan app."
            )
            return None

        # Slippage calculation
        actual_entry_price = float(actual_entry_price)
        slippage_pts = actual_entry_price - entry_price if side == "BUY" else entry_price - actual_entry_price
        slippage_pct = (slippage_pts / entry_price) * 100
        slippage_rs  = slippage_pts * qty
        self.logger.info(
            f"[{symbol}] Slippage: {slippage_pts:+.2f} pts ({slippage_pct:+.3f}%) "
            f"| Signal: ₹{entry_price} | Filled: ₹{actual_entry_price} | Cost: ₹{slippage_rs:+.2f}"
        )
        if abs(slippage_pct) > 0.3:
            self.logger.warning(f"[{symbol}] HIGH SLIPPAGE: {slippage_pct:+.3f}% — low liquidity suspected")

        # Recalculate TP1 from actual fill price to maintain correct 1:1.75 RR
        # Using signal price gives wrong RR when fill differs due to slippage
        actual_risk = abs(actual_entry_price - sl_price)
        if actual_risk > 0:
            tp1_actual = actual_entry_price + (cfg.TP1_REWARD_RATIO * actual_risk) if side == "BUY" \
                    else actual_entry_price - (cfg.TP1_REWARD_RATIO * actual_risk)
            self.logger.info(
                f"[{symbol}] TP1 adjusted: ₹{tp1:.2f} → ₹{tp1_actual:.2f} "
                f"(fill ₹{actual_entry_price} | risk ₹{actual_risk:.2f} | RR 1:{cfg.TP1_REWARD_RATIO})"
            )
            tp1 = tp1_actual

        trade = ActiveTrade(
            symbol=symbol,
            side=side,
            qty=qty,
            entry_price=actual_entry_price,
            entry_order_id=entry_orderid,
            sl_order_id="SOFTWARE_SL",
            stop_loss=sl_price,
            tp1=tp1,
            remaining_qty=qty,
            trail_sl=sl_price
        )
        self.state_manager.add_trade(trade)
        self._send_alert("LIVE ENTRY FILLED", trade, slippage_pts=slippage_pts, slippage_pct=slippage_pct)
        return trade

    # =========================================================
    # TRADE MANAGEMENT (called every 5 seconds)
    # =========================================================

    def manage_open_trade(self, trade: ActiveTrade, ltp: float) -> Tuple[ActiveTrade, bool]:
        """
        Returns (updated_trade, is_closed).

        Exit logic — full exit at SL or TP1:
          SL hit  → MARKET exit immediately
          TP1 hit → MARKET exit immediately
        """
        closed = False

        # ── PAPER ─────────────────────────────────────────────
        if cfg.PAPER_TRADE:
            sl_hit = (
                (trade.side == "BUY"  and ltp <= trade.stop_loss) or
                (trade.side == "SELL" and ltp >= trade.stop_loss)
            )
            if sl_hit:
                trade.pnl += (
                    (trade.stop_loss - trade.entry_price) * trade.qty
                    if trade.side == "BUY"
                    else (trade.entry_price - trade.stop_loss) * trade.qty
                )
                self.logger.info(f"[{trade.symbol}] PAPER SL HIT at {trade.stop_loss}.")
                self._send_alert("PAPER SL HIT", trade)
                return trade, True

            tp1_hit = (
                (trade.side == "BUY"  and ltp >= trade.tp1) or
                (trade.side == "SELL" and ltp <= trade.tp1)
            )
            if tp1_hit:
                trade.pnl += (
                    (trade.tp1 - trade.entry_price) * trade.qty
                    if trade.side == "BUY"
                    else (trade.entry_price - trade.tp1) * trade.qty
                )
                self.logger.info(f"[{trade.symbol}] PAPER TP1 HIT at {trade.tp1}.")
                self._send_alert("PAPER TP1 FULL EXIT", trade)
                return trade, True

            return trade, False

        # ── LIVE ──────────────────────────────────────────────
        # SL check — software monitored via LTP
        sl_hit = (
            (trade.side == "BUY"  and ltp <= trade.stop_loss) or
            (trade.side == "SELL" and ltp >= trade.stop_loss)
        )
        if sl_hit:
            exit_price = self._live_market_exit(trade, ltp, "SL")
            trade.pnl += (
                (exit_price - trade.entry_price) * trade.qty
                if trade.side == "BUY"
                else (trade.entry_price - exit_price) * trade.qty
            )
            self.logger.info(f"[{trade.symbol}] LIVE SL HIT at {exit_price}. Exit order sent.")
            self._send_alert("LIVE SL HIT", trade)
            return trade, True

        # TP1 check
        tp1_hit = (
            (trade.side == "BUY"  and ltp >= trade.tp1) or
            (trade.side == "SELL" and ltp <= trade.tp1)
        )
        if tp1_hit:
            exit_price = self._live_market_exit(trade, ltp, "TP1")
            trade.pnl += (
                (exit_price - trade.entry_price) * trade.qty
                if trade.side == "BUY"
                else (trade.entry_price - exit_price) * trade.qty
            )
            self.logger.info(f"[{trade.symbol}] LIVE TP1 HIT at {exit_price}. Exit order sent.")
            self._send_alert("LIVE TP1 FULL EXIT", trade)
            return trade, True

        return trade, False

    # =========================================================
    # FORCE SQUARE OFF (15:15)
    # =========================================================

    def square_off_all(self):
        trades = list(self.state_manager.state.active_trades.values())
        for trade in trades:
            if cfg.PAPER_TRADE:
                self.logger.info(f"[{trade.symbol}] PAPER FORCE SQUARE OFF")
                self._send_alert("PAPER FORCE SQUARE OFF", trade)
            else:
                exit_txn = "SELL" if trade.side == "BUY" else "BUY"
                try:
                    self.tsl.order_placement(
                        tradingsymbol=trade.symbol,
                        exchange="NSE",
                        quantity=trade.qty,
                        price=0,
                        trigger_price=0,
                        order_type="MARKET",
                        transaction_type=exit_txn,
                        trade_type="MIS"
                    )
                    self.logger.info(f"[{trade.symbol}] LIVE FORCE SQUARE OFF")
                    self._send_alert("LIVE FORCE SQUARE OFF", trade)
                except Exception as e:
                    self.logger.error(f"[{trade.symbol}] Square off failed: {e}")
            self.state_manager.remove_trade(trade.symbol)
