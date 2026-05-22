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
        self._exit_alert_sent = set()  # track symbols where exit-fail alert already sent

    def _mock_order_id(self, prefix: str) -> str:
        return f"{prefix}_{int(pytime.time() * 1000)}"

    def _send_alert(self, title: str, trade: ActiveTrade, slippage_pts: float = 0.0, slippage_pct: float = 0.0):
        try:
            is_entry = "ENTRY" in title
            is_sl    = "SL" in title
            is_tp    = "TP1" in title
            is_sq    = "SQUARE" in title

            if is_entry:
                direction = "LONG (BUY)" if trade.side == "BUY" else "SHORT (SELL)"
                slip_flag = "High" if abs(slippage_pct) > 0.3 else "Normal"
                msg = (
                    f"ENTRY - {trade.symbol}\n"
                    f"Direction : {direction}\n"
                    f"Qty       : {trade.qty}\n"
                    f"Entry     : Rs{trade.entry_price:.2f}\n"
                    f"Stop Loss : Rs{trade.stop_loss:.2f}\n"
                    f"Target    : Rs{trade.tp1:.2f}\n"
                    f"RR        : 1:1.75\n"
                    f"Slippage  : {slippage_pts:+.2f} pts ({slippage_pct:+.3f}%) {slip_flag}"
                )
            elif is_tp:
                msg = (
                    f"TARGET HIT - {trade.symbol}\n"
                    f"Qty   : {trade.qty}\n"
                    f"Entry : Rs{trade.entry_price:.2f}\n"
                    f"Target: Rs{trade.tp1:.2f}\n"
                    f"PnL   : Rs{trade.pnl:.2f}"
                )
            elif is_sl:
                msg = (
                    f"STOP LOSS HIT - {trade.symbol}\n"
                    f"Qty   : {trade.qty}\n"
                    f"Entry : Rs{trade.entry_price:.2f}\n"
                    f"SL    : Rs{trade.stop_loss:.2f}\n"
                    f"PnL   : Rs{trade.pnl:.2f}"
                )
            elif is_sq:
                msg = (
                    f"FORCE EXIT - {trade.symbol}\n"
                    f"Qty : {trade.qty}\n"
                    f"PnL : Rs{trade.pnl:.2f}"
                )
            else:
                msg = f"[{title}] {trade.symbol}"

            self._send_raw_alert(msg)
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
        """No-op — reconnect is handled by main.py which also updates executor.tsl."""
        self.logger.info("ExecutionEngine: reconnect deferred to main loop.")
        return False

    def _place_order(self, symbol, qty, price, trigger_price, order_type, txn_type):
        """Single order placement call."""
        return self.tsl.order_placement(
            tradingsymbol=symbol, exchange="NSE", quantity=qty,
            price=price, trigger_price=trigger_price,
            order_type=order_type, transaction_type=txn_type, trade_type="MIS"
        )

    def _live_market_exit(self, trade: ActiveTrade, ltp: float, reason: str) -> Optional[float]:
        """
        Single MARKET exit attempt.
        - Order placed and fill confirmed  → return fill price, clear alert flag
        - Order placed, fill not confirmed → return ltp as approximation (order IS in market)
        - Order placement failed entirely  → return None, send URGENT alert once
          On None: trade stays in state, fast loop retries every 5 seconds.
        """
        exit_txn = "SELL" if trade.side == "BUY" else "BUY"
        try:
            self.logger.info(f"[{trade.symbol}] Exit | MARKET | reason={reason} | ltp={ltp}")
            order_id = self._place_order(trade.symbol, trade.qty, 0, 0, "MARKET", exit_txn)
            if order_id:
                pytime.sleep(1)
                exit_price = self.tsl.get_executed_price(orderid=order_id)
                self._exit_alert_sent.discard(trade.symbol)
                if exit_price:
                    self.logger.info(f"[{trade.symbol}] Exit filled at ₹{exit_price}")
                    return float(exit_price)
                # Order placed but fill not yet confirmed — use ltp to avoid double exit
                self.logger.warning(f"[{trade.symbol}] Exit placed but fill unconfirmed. Using LTP ₹{ltp}")
                return float(ltp)
        except Exception as e:
            self.logger.error(f"[{trade.symbol}] Exit failed: {e}")

        # Order placement failed — keep trade in state, retry next 5s cycle
        self.logger.critical(f"[{trade.symbol}] EXIT FAILED — will retry next cycle.")
        if trade.symbol not in self._exit_alert_sent:
            self._send_raw_alert(
                f"URGENT — EXIT FAILED\n"
                f"Stock  : {trade.symbol}\n"
                f"Reason : {reason}\n"
                f"Side   : {trade.side} | Qty: {trade.qty}\n"
                f"Engine will retry every 5s. Check DHAN APP if this repeats."
            )
            self._exit_alert_sent.add(trade.symbol)
        return None

    # =========================================================
    # TICK SIZE HELPER
    # =========================================================

    def _get_tick_size(self, symbol: str, price: float = 0.0) -> float:
        """
        NSE tiered tick sizes:
          < ₹250   → ₹0.01 |  ₹250–₹1000  → ₹0.05
          ₹1000–₹5000 → ₹0.10 | ₹5000–₹10000 → ₹0.50
          ₹10000–₹20000 → ₹1.00 | > ₹20000 → ₹5.00
        Uses instrument file first (max across rows), falls back to price-based tier.
        """
        try:
            inst = getattr(self.tsl, "instrument_df", None)
            if inst is not None and not inst.empty:
                rows = inst[inst["SEM_TRADING_SYMBOL"] == symbol]
                if not rows.empty:
                    tick_paise = float(rows["SEM_TICK_SIZE"].max())
                    tick_rs = tick_paise / 100.0
                    if tick_rs > 0:
                        return tick_rs
        except Exception:
            pass
        if price > 0:
            if price < 250:   return 0.01
            if price < 1000:  return 0.05
            if price < 5000:  return 0.10
            if price < 10000: return 0.50
            if price < 20000: return 1.00
            return 5.00
        return 0.10

    def _round_to_tick(self, price: float, tick: float) -> float:
        """Round price to nearest tick using integer arithmetic.
        Avoids EXCH:16283 — float 5512.6000000003 rejected by NSE.
        """
        tick_paise  = round(tick * 100)
        price_paise = round(price * 100)
        rounded     = round(price_paise / tick_paise) * tick_paise
        return rounded / 100

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
        tick      = self._get_tick_size(symbol, entry_price)
        raw_limit = entry_price * 1.005 if side == "BUY" else entry_price * 0.995
        limit_price = self._round_to_tick(raw_limit, tick)
        self.logger.info(f"[{symbol}] Tick size: ₹{tick} | Limit price: ₹{limit_price}")

        # ── Single LIMIT entry attempt ────────────────────────
        entry_orderid      = None
        actual_entry_price = None

        try:
            self.logger.info(f"[{symbol}] Entry | LIMIT @ ₹{limit_price}")
            entry_orderid = self._place_order(symbol, qty, limit_price, 0, "LIMIT", entry_txn)

            if not entry_orderid:
                raise ValueError("No order ID returned")

            pytime.sleep(2)
            order_status = self.tsl.get_order_status(orderid=entry_orderid)
            if str(order_status).upper() in {"REJECTED", "CANCELLED", "FAILED", "INVALID"}:
                raise ValueError(f"Order {order_status}")

            actual_entry_price = self.tsl.get_executed_price(orderid=entry_orderid)

            if not actual_entry_price:
                # Still pending after 2s — cancel to avoid untracked fill later
                try:
                    self.tsl.cancel_order(OrderID=entry_orderid)
                    self.logger.warning(f"[{symbol}] Entry pending after 2s — cancelled.")
                except Exception as ce:
                    self.logger.error(f"[{symbol}] Cancel failed: {ce}")
                raise ValueError("Order pending after 2s — cancelled and skipped")

        except Exception as e:
            self.logger.error(f"[{symbol}] Entry failed: {e}")
            self._send_raw_alert(
                f"ENTRY FAILED — {symbol}\n"
                f"Reason : {e}\n"
                f"Trade NOT placed."
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
            if exit_price is None:
                return trade, False  # exit failed — keep in state, retry next cycle
            trade.pnl += (
                (exit_price - trade.entry_price) * trade.qty
                if trade.side == "BUY"
                else (trade.entry_price - exit_price) * trade.qty
            )
            self.logger.info(f"[{trade.symbol}] LIVE SL HIT at {exit_price}.")
            self._send_alert("LIVE SL HIT", trade)
            return trade, True

        # TP1 check
        tp1_hit = (
            (trade.side == "BUY"  and ltp >= trade.tp1) or
            (trade.side == "SELL" and ltp <= trade.tp1)
        )
        if tp1_hit:
            exit_price = self._live_market_exit(trade, ltp, "TP1")
            if exit_price is None:
                return trade, False  # exit failed — keep in state, retry next cycle
            trade.pnl += (
                (exit_price - trade.entry_price) * trade.qty
                if trade.side == "BUY"
                else (trade.entry_price - exit_price) * trade.qty
            )
            self.logger.info(f"[{trade.symbol}] LIVE TP1 HIT at {exit_price}.")
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
                self.state_manager.remove_trade(trade.symbol)
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
                    self.state_manager.remove_trade(trade.symbol)
                except Exception as e:
                    self.logger.error(f"[{trade.symbol}] Square off failed: {e}")
                    self._send_raw_alert(
                        f"URGENT — FORCE EXIT FAILED\n"
                        f"Stock  : {trade.symbol}\n"
                        f"Side   : {trade.side} | Qty: {trade.qty}\n"
                        f"Error  : {e}\n"
                        f"Engine will retry. Check DHAN APP immediately."
                    )
