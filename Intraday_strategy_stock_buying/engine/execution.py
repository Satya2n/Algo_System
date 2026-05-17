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
        Returns (updated_trade, is_closed)
        """
        closed = False

        if getattr(trade, "pending_sl_update", False) and not cfg.PAPER_TRADE:
            exit_txn = "SELL" if trade.side == "BUY" else "BUY"
            new_sl_id = self.tsl.order_placement(
                tradingsymbol=trade.symbol, exchange="NSE", quantity=trade.remaining_qty, price=0, trigger_price=trade.pending_sl_price,
                order_type="STOPMARKET", transaction_type=exit_txn, trade_type="MIS"
            )
            if new_sl_id:
                trade.sl_order_id = new_sl_id
                trade.trail_sl = trade.pending_sl_price
                trade.pending_sl_update = False
                trade.pending_sl_price = 0.0
                self.logger.info(f"[{trade.symbol}] LIVE SL successfully placed at {trade.trail_sl}")
                self.state_manager.update_trade(trade)
                self._send_alert("TRAIL SL PLACED", trade)
            else:
                self.logger.warning(f"[{trade.symbol}] LIVE SL replacement failed. Will retry.")
            
            return trade, closed

        if cfg.PAPER_TRADE:
            # Paper trade SL check
            sl_hit = (trade.side == "BUY" and ltp <= trade.trail_sl) or (trade.side == "SELL" and ltp >= trade.trail_sl)
            if sl_hit:
                trade.pnl += (trade.trail_sl - trade.entry_price) * trade.remaining_qty if trade.side == "BUY" else (trade.entry_price - trade.trail_sl) * trade.remaining_qty
                self.logger.info(f"[{trade.symbol}] PAPER SL HIT at {trade.trail_sl}. Trade closed.")
                self._send_alert("PAPER SL HIT", trade)
                closed = True
                return trade, closed

            # Paper trade TP1 check
            tp1_hit = (trade.side == "BUY" and ltp >= trade.tp1) or (trade.side == "SELL" and ltp <= trade.tp1)
            if not trade.partial_done and tp1_hit:
                partial_qty = max(1, trade.qty // 2)
                partial_pnl = (ltp - trade.entry_price) * partial_qty if trade.side == "BUY" else (trade.entry_price - ltp) * partial_qty
                
                trade.partial_done = True
                trade.remaining_qty = trade.qty - partial_qty
                trade.pnl += partial_pnl
                trade.trail_sl = trade.entry_price # Move to breakeven
                self.logger.info(f"[{trade.symbol}] PAPER TP1 HIT. Booked 50%. SL moved to BE.")
                self.state_manager.update_trade(trade)
                self._send_alert("PAPER TP1 HIT", trade)

            # Paper trailing
            if trade.partial_done:
                trail_sl = self.get_trailing_stop(live_df, trade.side)
                if trail_sl:
                    if (trade.side == "BUY" and trail_sl > trade.trail_sl) or (trade.side == "SELL" and trail_sl < trade.trail_sl):
                        trade.trail_sl = round(trail_sl, 1)
                        self.logger.info(f"[{trade.symbol}] PAPER Trailing SL updated to {trade.trail_sl}")
                        self.state_manager.update_trade(trade)
                        self._send_alert("PAPER TRAIL UPDATED", trade)
            
            return trade, closed

        # ================= LIVE MANAGEMENT =================
        # 1. Check if SL was hit
        status = self.tsl.get_order_status(orderid=trade.sl_order_id)
        if status == "TRADED":
            exit_price = self.tsl.get_executed_price(orderid=trade.sl_order_id)
            if exit_price is None: exit_price = trade.trail_sl # NoneType Math crash fix
            
            trail_pnl = (exit_price - trade.entry_price) * trade.remaining_qty if trade.side == "BUY" else (trade.entry_price - exit_price) * trade.remaining_qty
            trade.pnl += trail_pnl
            self.logger.info(f"[{trade.symbol}] LIVE SL HIT. Trade closed.")
            self._send_alert("LIVE SL HIT", trade)
            closed = True
            return trade, closed

        # 2. Check TP1
        tp1_hit = (trade.side == "BUY" and ltp >= trade.tp1) or (trade.side == "SELL" and ltp <= trade.tp1)
        if not trade.partial_done and tp1_hit:
            partial_qty = max(1, trade.qty // 2)
            exit_txn = "SELL" if trade.side == "BUY" else "BUY"
            
            limit_price = round(ltp * 0.99 * 20) / 20 if exit_txn == "SELL" else round(ltp * 1.01 * 20) / 20
            
            # Fire limit order for 50%
            exit_orderid = self.tsl.order_placement(
                tradingsymbol=trade.symbol, exchange="NSE", quantity=partial_qty, price=limit_price, trigger_price=0,
                order_type="LIMIT", transaction_type=exit_txn, trade_type="MIS"
            )
            partial_exit_price = self.tsl.get_executed_price(orderid=exit_orderid)
            if partial_exit_price is None: partial_exit_price = ltp # NoneType Math Crash fix
            
            partial_pnl = (partial_exit_price - trade.entry_price) * partial_qty if trade.side == "BUY" else (trade.entry_price - partial_exit_price) * partial_qty
            
            trade.partial_done = True
            trade.remaining_qty -= partial_qty
            trade.pnl += partial_pnl
            
            # Ghost Order Fix: Cancel old SL, flag trade for async SL placement
            self.tsl.cancel_order(OrderID=trade.sl_order_id)
            
            breakeven_sl = round(trade.entry_price, 1)
            trade.pending_sl_update = True
            trade.pending_sl_price = breakeven_sl
            trade.trail_sl = breakeven_sl
            
            self.logger.info(f"[{trade.symbol}] LIVE TP1 HIT. Booked 50%. SL cancel sent. SL update pending.")
            self.state_manager.update_trade(trade)
            self._send_alert("TP1 BOOKED", trade)

        # 3. Trailing SL
        if trade.partial_done and not getattr(trade, "pending_sl_update", False):
            trail_sl = self.get_trailing_stop(live_df, trade.side)
            if trail_sl:
                if (trade.side == "BUY" and trail_sl > trade.trail_sl) or (trade.side == "SELL" and trail_sl < trade.trail_sl):
                    # Ghost Order Fix
                    self.tsl.cancel_order(OrderID=trade.sl_order_id)
                    
                    trade.pending_sl_update = True
                    trade.pending_sl_price = round(trail_sl, 1)
                    trade.trail_sl = round(trail_sl, 1)

                    self.logger.info(f"[{trade.symbol}] LIVE Trailing SL cancel sent. SL update pending -> {trade.trail_sl}")
                    self.state_manager.update_trade(trade)

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
