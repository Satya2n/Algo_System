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
        """Send a plain text alert not tied to a trade object."""
        from config.credentials import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
        try:
            self.tsl.send_telegram_alert(
                message=message,
                receiver_chat_id=TELEGRAM_CHAT_ID,
                bot_token=TELEGRAM_BOT_TOKEN
            )
        except Exception:
            pass

    def _live_market_exit(self, trade: ActiveTrade, ltp: float, reason: str) -> float:
        """Place a MARKET exit order and return executed price."""
        exit_txn = "SELL" if trade.side == "BUY" else "BUY"
        try:
            exit_orderid = self.tsl.order_placement(
                tradingsymbol=trade.symbol,
                exchange="NSE",
                quantity=trade.qty,
                price=0,
                trigger_price=0,
                order_type="MARKET",
                transaction_type=exit_txn,
                trade_type="MIS"
            )
            exit_price = self.tsl.get_executed_price(orderid=exit_orderid)
            if exit_price:
                return float(exit_price)
        except Exception as e:
            self.logger.error(f"[{trade.symbol}] Market exit failed: {e}")
        return ltp  # fallback to LTP if fetch fails

    # =========================================================
    # TICK SIZE HELPER
    # =========================================================

    def _get_tick_size(self, symbol: str) -> float:
        """Fetch tick size from Dhan instrument file. Defaults to ₹0.05."""
        try:
            inst = getattr(self.tsl, "instrument_df", None)
            if inst is not None and not inst.empty:
                row = inst[inst["SEM_TRADING_SYMBOL"] == symbol]
                if not row.empty:
                    tick = float(row.iloc[0].get("SEM_TICK_SIZE", 0.05))
                    if tick > 0:
                        return tick
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

        entry_orderid = self.tsl.order_placement(
            tradingsymbol=symbol,
            exchange="NSE",
            quantity=qty,
            price=limit_price,
            trigger_price=0,
            order_type="LIMIT",
            transaction_type=entry_txn,
            trade_type="MIS"
        )

        if not entry_orderid:
            self.logger.error(f"[{symbol}] Entry order rejected by exchange.")
            self._send_raw_alert(f"❌ ORDER REJECTED — {symbol}\nReason: Exchange rejected entry order\nPrice tried: ₹{limit_price}")
            return None

        # Wait briefly then verify order actually filled
        pytime.sleep(1.5)
        order_status = self.tsl.get_order_status(orderid=entry_orderid)
        if str(order_status).upper() in {"REJECTED", "CANCELLED", "FAILED", "INVALID"}:
            self.logger.error(f"[{symbol}] Entry order REJECTED: {order_status}")
            self._send_raw_alert(f"❌ ORDER REJECTED — {symbol}\nStatus: {order_status}\nPrice tried: ₹{limit_price}")
            return None

        actual_entry_price = self.tsl.get_executed_price(orderid=entry_orderid)
        if not actual_entry_price:
            self.logger.error(f"[{symbol}] Order placed but no fill confirmed. Aborting to prevent orphan.")
            self._send_raw_alert(f"⚠️ ORDER NOT CONFIRMED — {symbol}\nNo fill returned. Trade NOT added. Check Dhan app.")
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

        trade = ActiveTrade(
            symbol=symbol,
            side=side,
            qty=qty,
            entry_price=float(actual_entry_price),
            entry_order_id=entry_orderid,
            sl_order_id="SOFTWARE_SL",   # SL monitored in software, not on exchange
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
            self.logger.info(f"[{trade.symbol}] LIVE SL HIT at {exit_price}. MARKET exit placed.")
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
            self.logger.info(f"[{trade.symbol}] LIVE TP1 HIT at {exit_price}. MARKET exit placed.")
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
