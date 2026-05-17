# engine/execution.py
import logging
import time as pytime
import pandas as pd
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

    def _send_alert(self, title: str, trade: ActiveTrade):
        from config.credentials import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
        try:
            msg = f"[{title}]\nSymbol: {trade.symbol}\nSide: {trade.side}\nQty: {trade.qty}\nEntry: {trade.entry_price}\nSL: {trade.trail_sl}\nPnL: {trade.pnl}"
            self.tsl.send_telegram_alert(message=msg, receiver_chat_id=TELEGRAM_CHAT_ID, bot_token=TELEGRAM_BOT_TOKEN)
        except Exception:
            pass

    def place_initial_orders(self, symbol: str, side: str, qty: int, entry_price: float, sl_price: float, tp1: float) -> Optional[ActiveTrade]:
        if qty <= 0:
            return None

        self.logger.info(f"[{symbol}] Attempting Entry | Side: {side} | Qty: {qty} | Entry: {entry_price} | SL: {sl_price}")

        if cfg.PAPER_TRADE:
            trade = ActiveTrade(
                symbol=symbol,
                side=side,
                qty=qty,
                entry_price=entry_price,
                entry_order_id=self._mock_order_id("PAPER_ENT"),
                sl_order_id=self._mock_order_id("PAPER_SL"),
                stop_loss=sl_price,
                tp1=tp1,
                remaining_qty=qty,
                trail_sl=sl_price
            )
            self.state_manager.add_trade(trade)
            self._send_alert("PAPER ENTRY FILLED", trade)
            return trade

        # LIVE EXECUTION
        entry_txn = "BUY" if side == "BUY" else "SELL"
        exit_txn = "SELL" if side == "BUY" else "BUY"

        limit_price = round(entry_price * 1.01 * 20) / 20 if side == "BUY" else round(entry_price * 0.99 * 20) / 20

        # 1. Place Entry
        entry_orderid = self.tsl.order_placement(
            tradingsymbol=symbol, exchange="NSE", quantity=qty, price=limit_price, trigger_price=0,
            order_type="LIMIT", transaction_type=entry_txn, trade_type="MIS"
        )

        if not entry_orderid:
            self.logger.error(f"[{symbol}] Entry MARKET order rejected by exchange.")
            return None

        # Fetch actual executed price
        actual_entry_price = self.tsl.get_executed_price(orderid=entry_orderid)
        if not actual_entry_price:
            self.logger.warning(f"[{symbol}] Could not fetch executed price, using theoretical.")
            actual_entry_price = entry_price

        # 2. Place Stop Loss (ORPHAN PROTECTION LOGIC)
        sl_orderid = self.tsl.order_placement(
            tradingsymbol=symbol, exchange="NSE", quantity=qty, price=0, trigger_price=sl_price,
            order_type="STOPMARKET", transaction_type=exit_txn, trade_type="MIS"
        )

        if not sl_orderid:
            self.logger.critical(f"[{symbol}] SL ORDER REJECTED! Orphan position detected. Initiating emergency flatting.")
            self.tsl.order_placement(
                tradingsymbol=symbol, exchange="NSE", quantity=qty, price=0, trigger_price=0,
                order_type="MARKET", transaction_type=exit_txn, trade_type="MIS"
            )
            return None

        trade = ActiveTrade(
            symbol=symbol,
            side=side,
            qty=qty,
            entry_price=actual_entry_price,
            entry_order_id=entry_orderid,
            sl_order_id=sl_orderid,
            stop_loss=sl_price,
            tp1=tp1,
            remaining_qty=qty,
            trail_sl=sl_price
        )
        self.state_manager.add_trade(trade)
        self._send_alert("ENTRY FILLED", trade)
        return trade

    def get_trailing_stop(self, live_df: pd.DataFrame, side: str) -> Optional[float]:
        if live_df is None or len(live_df) < 3:
            return None
        last3 = live_df.tail(3)
        if side == "BUY":
            return float(last3["low"].min())
        else:
            return float(last3["high"].max())

    def manage_open_trade(self, trade: ActiveTrade, ltp: float, live_df: pd.DataFrame) -> Tuple[ActiveTrade, bool]:
        """
        Returns (updated_trade, is_closed).

        Exit logic — full exit, no partial booking, no trailing:
          SL hit  → exit 100% at SL price  → closed
          TP1 hit → exit 100% at TP1 price → closed
        """
        closed = False

        if cfg.PAPER_TRADE:
            # SL check
            sl_hit = (trade.side == "BUY" and ltp <= trade.stop_loss) or \
                     (trade.side == "SELL" and ltp >= trade.stop_loss)
            if sl_hit:
                trade.pnl += (trade.stop_loss - trade.entry_price) * trade.qty if trade.side == "BUY" \
                         else (trade.entry_price - trade.stop_loss) * trade.qty
                self.logger.info(f"[{trade.symbol}] PAPER SL HIT at {trade.stop_loss}. Trade closed.")
                self._send_alert("PAPER SL HIT", trade)
                closed = True
                return trade, closed

            # TP1 check — full exit
            tp1_hit = (trade.side == "BUY" and ltp >= trade.tp1) or \
                      (trade.side == "SELL" and ltp <= trade.tp1)
            if tp1_hit:
                trade.pnl += (trade.tp1 - trade.entry_price) * trade.qty if trade.side == "BUY" \
                         else (trade.entry_price - trade.tp1) * trade.qty
                self.logger.info(f"[{trade.symbol}] PAPER TP1 HIT at {trade.tp1}. Full exit.")
                self._send_alert("PAPER TP1 FULL EXIT", trade)
                closed = True
                return trade, closed

            return trade, closed

        # ================= LIVE MANAGEMENT =================
        # 1. Check if SL was hit
        status = self.tsl.get_order_status(orderid=trade.sl_order_id)
        if status == "TRADED":
            exit_price = self.tsl.get_executed_price(orderid=trade.sl_order_id)
            if exit_price is None:
                exit_price = trade.stop_loss
            trade.pnl += (exit_price - trade.entry_price) * trade.qty if trade.side == "BUY" \
                     else (trade.entry_price - exit_price) * trade.qty
            self.logger.info(f"[{trade.symbol}] LIVE SL HIT at {exit_price}. Trade closed.")
            self._send_alert("LIVE SL HIT", trade)
            closed = True
            return trade, closed

        # 2. TP1 hit — cancel SL, full exit
        tp1_hit = (trade.side == "BUY" and ltp >= trade.tp1) or \
                  (trade.side == "SELL" and ltp <= trade.tp1)
        if tp1_hit:
            exit_txn = "SELL" if trade.side == "BUY" else "BUY"
            limit_price = round(ltp * 0.99 * 20) / 20 if exit_txn == "SELL" \
                     else round(ltp * 1.01 * 20) / 20

            # Cancel the SL order first
            try:
                self.tsl.cancel_order(OrderID=trade.sl_order_id)
            except Exception:
                pass

            # Full exit
            exit_orderid = self.tsl.order_placement(
                tradingsymbol=trade.symbol, exchange="NSE", quantity=trade.qty,
                price=limit_price, trigger_price=0,
                order_type="LIMIT", transaction_type=exit_txn, trade_type="MIS"
            )
            exit_price = self.tsl.get_executed_price(orderid=exit_orderid)
            if exit_price is None:
                exit_price = ltp

            trade.pnl += (exit_price - trade.entry_price) * trade.qty if trade.side == "BUY" \
                     else (trade.entry_price - exit_price) * trade.qty
            self.logger.info(f"[{trade.symbol}] LIVE TP1 HIT at {exit_price}. Full exit.")
            self._send_alert("LIVE TP1 FULL EXIT", trade)
            closed = True
            return trade, closed

        return trade, closed

    def square_off_all(self):
        trades = list(self.state_manager.state.active_trades.values())
        for trade in trades:
            if cfg.PAPER_TRADE:
                self.logger.info(f"[{trade.symbol}] PAPER FORCE SQUARE OFF")
                self._send_alert("PAPER FORCE SQUARE OFF", trade)
            else:
                try:
                    self.tsl.cancel_order(OrderID=trade.sl_order_id)
                except Exception:
                    pass
                exit_txn = "SELL" if trade.side == "BUY" else "BUY"
                self.tsl.order_placement(
                    tradingsymbol=trade.symbol, exchange="NSE", quantity=trade.remaining_qty, price=0, trigger_price=0,
                    order_type="MARKET", transaction_type=exit_txn, trade_type="MIS"
                )
                self.logger.info(f"[{trade.symbol}] LIVE FORCE SQUARE OFF")
                self._send_alert("LIVE FORCE SQUARE OFF", trade)
            self.state_manager.remove_trade(trade.symbol)
