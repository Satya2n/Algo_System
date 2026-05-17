"""
Execution layer for the positional NIFTY options engine.

Responsibilities:
- Convert strategy signals into a spread plan
- Paper trade in backtests
- Place live orders in live mode
- Track open trade state
- Save trade book and state
- Send Telegram alerts
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import pandas as pd

from engine.state import ActiveTrade, EngineState, StateStore
from engine.risk import RiskManager
from engine.telegram_control import TelegramConfig, TelegramNotifier


# =========================================================
# CONFIG / RESULT
# =========================================================

@dataclass
class ExecutionConfig:
    symbol: str = "NIFTY"
    exchange: str = "INDEX"

    paper_trade: bool = True

    expiry_index: int = 0
    option_chain_strikes: int = 20

    short_leg_delta: float = 0.25
    hedge_leg_delta: float = 0.10

    trade_product: str = "MARGIN"
    order_type: str = "LIMIT"
    validity: str = "DAY"

    limit_buffer_pct: float = 0.02
    min_absolute_buffer: float = 2.0

    target_profit_pct: float = 0.65
    stop_loss_pct: float = 0.85
    trail_to_breakeven_pct: float = 0.35
    exit_on_opposite_signal: bool = True

    max_order_retries: int = 2
    max_wait_for_fill_seconds: float = 20.0

    trade_book_path: str = "state/trade_book.csv"


@dataclass
class ExecutionResult:
    ok: bool
    message: str
    trade: Optional[ActiveTrade] = None
    payload: Dict[str, Any] = field(default_factory=dict)


# =========================================================
# ENGINE
# =========================================================

class ExecutionEngine:
    def __init__(
        self,
        tsl: Any,
        risk: RiskManager,
        state_store: StateStore,
        cfg: ExecutionConfig,
        logger: Any,
        state: Optional[EngineState] = None,
    ):
        self.tsl = tsl
        self.risk = risk
        self.state_store = state_store
        self.cfg = cfg
        self.logger = logger

        self.state = state if state is not None else self.state_store.load()
        self.state.paper_trade = self.cfg.paper_trade
        self.state_store.save(self.state)

        self.telegram = TelegramNotifier(TelegramConfig(), logger=logger)

        self.trade_book_path = Path(self.cfg.trade_book_path)
        self.trade_book_path.parent.mkdir(parents=True, exist_ok=True)

    # -----------------------------------------------------
    # STATE HELPERS
    # -----------------------------------------------------
    def _save_state(self) -> None:
        self.state_store.save(self.state)

    def _set_in_trade(self, trade: ActiveTrade) -> None:
        self.state.trade_mode = "IN_TRADE"
        self.state.trade_taken_today = True
        self.state.today_trade_count += 1
        self.state.active_trade = trade
        self._save_state()

    def _set_waiting(self) -> None:
        self.state.trade_mode = "WAITING"
        self._save_state()

    def _set_cooldown(self) -> None:
        self.state.trade_mode = "COOLDOWN"
        self._save_state()

    def _clear_active_trade(self) -> None:
        self.state.active_trade = None
        self._save_state()

    def _append_trade_book(self, row: Dict[str, Any]) -> None:
        df = pd.DataFrame([row])
        header = not self.trade_book_path.exists()
        df.to_csv(self.trade_book_path, mode="a", header=header, index=False)

    # -----------------------------------------------------
    # SMALL HELPERS
    # -----------------------------------------------------
    def _step_for_symbol(self) -> int:
        sym = self.cfg.symbol.upper()
        if sym in {"NIFTY", "FINNIFTY", "MIDCPNIFTY"}:
            return 50
        if sym in {"BANKNIFTY", "BANKEX", "SENSEX"}:
            return 100
        return 50

    def _round_to_step(self, price: float, step: int) -> int:
        return int(round(price / step) * step)

    def _safe_send(self, text: str) -> None:
        self.telegram.send(text)

    def _safe_balance(self) -> float:
        try:
            return float(self.tsl.get_balance())
        except Exception as e:
            self.logger.exception(f"Balance fetch failed: {e}")
            return 0.0

    def _get_lot_size(self, symbol: str, fallback: int = 50) -> int:
        try:
            if hasattr(self.tsl, "get_lot_size"):
                ls = self.tsl.get_lot_size(symbol)
                if ls:
                    return int(ls)
        except Exception:
            pass

        inst = getattr(self.tsl, "instrument_df", None)
        if isinstance(inst, pd.DataFrame) and not inst.empty:
            for col in ["SEM_LOT_UNITS", "lot_size", "LOT_SIZE"]:
                if col in inst.columns:
                    try:
                        v = int(inst.iloc[-1][col])
                        if v > 0:
                            return v
                    except Exception:
                        pass

        return int(fallback)

    def _try_get_option_chain(self) -> Tuple[Optional[float], Optional[pd.DataFrame], str]:
        """
        Tries multiple possible signatures because wrapper implementations vary.
        Falls back safely if unavailable.
        """
        attempts = [
            lambda: self.tsl.get_option_chain(self.cfg.symbol, self.cfg.exchange, self.cfg.expiry_index, num_strikes=self.cfg.option_chain_strikes),
            lambda: self.tsl.get_option_chain(self.cfg.symbol, self.cfg.exchange, expiry=self.cfg.expiry_index, num_strikes=self.cfg.option_chain_strikes),
            lambda: self.tsl.get_option_chain(self.cfg.symbol, self.cfg.exchange, self.cfg.expiry_index),
        ]

        last_err = None
        for fn in attempts:
            try:
                result = fn()

                if isinstance(result, tuple):
                    if len(result) == 3:
                        atm, oc_df, expiry = result
                        return float(atm), pd.DataFrame(oc_df), str(expiry)
                    if len(result) == 2:
                        atm, oc_df = result
                        return float(atm), pd.DataFrame(oc_df), ""
                if isinstance(result, dict):
                    atm = result.get("atm_strike") or result.get("atm")
                    oc_df = result.get("data") or result.get("option_chain") or result.get("df")
                    expiry = result.get("expiry_date") or result.get("expiry") or ""
                    if atm is not None and oc_df is not None:
                        return float(atm), pd.DataFrame(oc_df), str(expiry)

            except Exception as e:
                last_err = e

        if last_err:
            self.logger.warning(f"Option chain fetch failed, using fallback plan: {last_err}")

        return None, None, ""

    def _select_by_delta(self, option_chain_df: pd.DataFrame, side: str, target_delta: float) -> pd.Series:
        df = option_chain_df.copy()
        side = side.upper()

        if side == "CE":
            col = "CE Delta"
        elif side == "PE":
            col = "PE Delta"
        else:
            raise ValueError("side must be CE or PE")

        if col not in df.columns or "Strike Price" not in df.columns:
            raise RuntimeError(f"Option chain missing required columns for {side}")

        df[col] = pd.to_numeric(df[col], errors="coerce")
        df["Strike Price"] = pd.to_numeric(df["Strike Price"], errors="coerce")
        df = df.dropna(subset=[col, "Strike Price"])

        if df.empty:
            raise RuntimeError(f"No valid option rows found for {side}")

        idx = (df[col].abs() - abs(target_delta)).abs().idxmin()
        return df.loc[idx]

    def _resolve_contract_symbol(self, strike: float, option_type: str, expiry_date: str) -> str:
        inst = getattr(self.tsl, "instrument_df", None)
        if not isinstance(inst, pd.DataFrame) or inst.empty:
            raise RuntimeError("instrument_df not available")

        df = inst.copy()
        option_type = option_type.upper()

        # Try common Dhan columns
        if "SEM_EXPIRY_DATE" in df.columns:
            df["SEM_EXPIRY_DATE"] = pd.to_datetime(df["SEM_EXPIRY_DATE"], errors="coerce")
            df["ContractExpiration"] = df["SEM_EXPIRY_DATE"].dt.date.astype(str)
        elif "SEM_EXPIRY" in df.columns:
            df["ContractExpiration"] = df["SEM_EXPIRY"].astype(str)
        else:
            df["ContractExpiration"] = ""

        if "SEM_STRIKE_PRICE" in df.columns:
            df["SEM_STRIKE_PRICE"] = pd.to_numeric(df["SEM_STRIKE_PRICE"], errors="coerce")
        else:
            df["SEM_STRIKE_PRICE"] = pd.to_numeric(df.get("strike_price", pd.Series(dtype=float)), errors="coerce")

        if "SEM_OPTION_TYPE" not in df.columns:
            raise RuntimeError("instrument_df missing SEM_OPTION_TYPE")

        df = df[
            (df["SEM_OPTION_TYPE"].astype(str).str.upper() == option_type)
            & (df["ContractExpiration"].astype(str) == str(expiry_date))
        ].copy()

        if df.empty:
            raise RuntimeError(f"Could not find {option_type} contract for expiry {expiry_date}")

        exact = df[df["SEM_STRIKE_PRICE"] == float(strike)]
        if exact.empty:
            df["diff"] = (df["SEM_STRIKE_PRICE"] - float(strike)).abs()
            exact = df.sort_values("diff").head(1)

        row = exact.iloc[-1]
        symbol = row.get("SEM_CUSTOM_SYMBOL") or row.get("SEM_TRADING_SYMBOL")
        if not symbol:
            raise RuntimeError("Could not resolve contract symbol")
        return str(symbol)

    def _synthetic_plan(self, signal: str, spot: float) -> Dict[str, Any]:
        step = self._step_for_symbol()
        atm = self._round_to_step(spot, step)

        if signal == "BULLISH_ENTRY":
            opt_side = "PE"
            short_strike = atm - 2 * step
            hedge_strike = atm - 4 * step
        else:
            opt_side = "CE"
            short_strike = atm + 2 * step
            hedge_strike = atm + 4 * step

        # Synthetic credit just for paper mode fallback
        net_credit = max(step * 0.06, 1.0)
        lot_size = self._get_lot_size(self.cfg.symbol, fallback=50)

        short_symbol = f"{self.cfg.symbol}_{opt_side}_{int(short_strike)}"
        hedge_symbol = f"{self.cfg.symbol}_{opt_side}_{int(hedge_strike)}"

        return {
            "signal": signal,
            "underlying": self.cfg.symbol,
            "spot": spot,
            "expiry_date": "",
            "option_side": opt_side,
            "short": {"symbol": short_symbol, "strike": float(short_strike), "delta": self.cfg.short_leg_delta, "ltp": net_credit * 1.8},
            "hedge": {"symbol": hedge_symbol, "strike": float(hedge_strike), "delta": self.cfg.hedge_leg_delta, "ltp": net_credit * 0.8},
            "spread_width": abs(short_strike - hedge_strike),
            "net_credit": float(net_credit),
            "lot_size": int(lot_size),
            "synthetic": True,
        }

    def build_spread_plan(self, signal_row: pd.Series) -> Dict[str, Any]:
        signal = str(signal_row["signal"])
        spot = float(signal_row["close"])

        # Try real option chain first
        atm, oc_df, expiry_date = self._try_get_option_chain()

        if atm is None or oc_df is None or oc_df.empty:
            return self._synthetic_plan(signal, spot)

        try:
            if signal == "BULLISH_ENTRY":
                opt_side = "PE"
                short_row = self._select_by_delta(oc_df, "PE", self.cfg.short_leg_delta)
                hedge_row = self._select_by_delta(oc_df, "PE", self.cfg.hedge_leg_delta)
            elif signal == "BEARISH_ENTRY":
                opt_side = "CE"
                short_row = self._select_by_delta(oc_df, "CE", self.cfg.short_leg_delta)
                hedge_row = self._select_by_delta(oc_df, "CE", self.cfg.hedge_leg_delta)
            else:
                raise ValueError(f"Unsupported signal: {signal}")

            short_strike = float(short_row["Strike Price"])
            hedge_strike = float(hedge_row["Strike Price"])

            short_ltp = float(short_row[f"{opt_side} LTP"])
            hedge_ltp = float(hedge_row[f"{opt_side} LTP"])

            short_delta = float(short_row[f"{opt_side} Delta"])
            hedge_delta = float(hedge_row[f"{opt_side} Delta"])

            short_symbol = self._resolve_contract_symbol(short_strike, opt_side, expiry_date)
            hedge_symbol = self._resolve_contract_symbol(hedge_strike, opt_side, expiry_date)

            lot_size = self._get_lot_size(short_symbol, fallback=50)

            return {
                "signal": signal,
                "underlying": self.cfg.symbol,
                "spot": spot,
                "expiry_date": expiry_date,
                "option_side": opt_side,
                "short": {"symbol": short_symbol, "strike": short_strike, "delta": short_delta, "ltp": short_ltp},
                "hedge": {"symbol": hedge_symbol, "strike": hedge_strike, "delta": hedge_delta, "ltp": hedge_ltp},
                "spread_width": abs(short_strike - hedge_strike),
                "net_credit": max(short_ltp - hedge_ltp, 0.01),
                "lot_size": int(lot_size),
                "synthetic": False,
            }

        except Exception as e:
            self.logger.warning(f"Real option plan failed, using fallback: {e}")
            return self._synthetic_plan(signal, spot)

    def _estimate_qty_and_risk(self, plan: Dict[str, Any]) -> Tuple[int, float]:
        spread_width = float(plan["spread_width"])
        lot_size = int(plan["lot_size"])
        net_credit = float(plan["net_credit"])

        # Determine how many lots we want to take based on our risk limits
        desired_qty_lots = self.risk.position_size_for_spread(
            spread_width=spread_width,
            lot_size=lot_size,
            net_credit=net_credit,
        )

        # But we must cap it based on available broker margin
        estimated_margin_required = self.risk.get_required_broker_margin(desired_qty_lots)
        available_margin = self._safe_balance()
        
        # Max margin we're allowed to use globally
        max_allowed_margin = self.risk.cfg.capital * self.risk.cfg.max_margin_utilization_pct
        effective_margin_limit = min(available_margin, max_allowed_margin)
        
        # Cap the lots if we don't have enough margin
        if estimated_margin_required > effective_margin_limit and self.risk.cfg.margin_per_lot > 0:
            desired_qty_lots = int(effective_margin_limit // self.risk.cfg.margin_per_lot)
            estimated_margin_required = self.risk.get_required_broker_margin(desired_qty_lots)

        qty_total = desired_qty_lots * lot_size

        return qty_total, estimated_margin_required

    def _paper_current_credit(self, trade: ActiveTrade, current_spot: float) -> float:
        entry_spot = float(trade.entry_spot)
        entry_credit = float(trade.entry_price)

        movement_pct = (current_spot - entry_spot) / max(entry_spot, 1e-9)

        if trade.side == "BULLISH_ENTRY":
            # Bull put spread benefits when spot rises
            current_credit = entry_credit * (1.0 - 1.5 * movement_pct)
        else:
            # Bear call spread benefits when spot falls
            current_credit = entry_credit * (1.0 + 1.5 * movement_pct)

        return max(float(current_credit), 0.01)

    def mark_to_market(self, signal_row: Optional[pd.Series] = None) -> Optional[float]:
        trade = self.state.active_trade
        if trade is None:
            return None

        if self.cfg.paper_trade:
            if signal_row is None or "close" not in signal_row:
                return trade.pnl

            current_spot = float(signal_row["close"])
            current_credit = self._paper_current_credit(trade, current_spot)
            entry_credit = float(trade.entry_price)
            pnl = (entry_credit - current_credit) * float(trade.qty)

            trade.pnl = round(pnl, 2)
            self.state.last_pnl = trade.pnl
            self.state.active_trade = trade
            self._save_state()
            return trade.pnl

        return trade.pnl

    def _should_exit(self, signal_row: Optional[pd.Series] = None) -> Tuple[bool, str]:
        trade = self.state.active_trade
        if trade is None:
            return False, "No active trade"

        current_pnl = self.mark_to_market(signal_row)
        if current_pnl is None:
            return False, "PnL unavailable"

        if self.cfg.paper_trade:
            current_spot = float(signal_row["close"]) if signal_row is not None and "close" in signal_row else float(trade.entry_spot)
            current_credit = self._paper_current_credit(trade, current_spot)
        else:
            current_credit = float(trade.entry_price)  # live exit uses leg fills, not this approximation

        entry_credit = float(trade.entry_price)

        if not getattr(trade, "sl_moved_to_breakeven", False):
            if current_credit <= entry_credit * (1.0 - self.cfg.trail_to_breakeven_pct):
                trade.sl_moved_to_breakeven = True
                self._save_state()
                self.logger.info(f"Trailing Stop Triggered: Moving SL to breakeven ({entry_credit})")

        target_value = entry_credit * (1.0 - self.cfg.target_profit_pct)
        
        if getattr(trade, "sl_moved_to_breakeven", False):
            stop_value = entry_credit
        else:
            stop_value = entry_credit * (1.0 + self.cfg.stop_loss_pct)

        if current_credit <= target_value:
            return True, "Target reached"

        if current_credit >= stop_value:
            return True, "Stop loss hit"

        if self.cfg.exit_on_opposite_signal and signal_row is not None:
            current_signal = str(signal_row.get("signal", "NO_TRADE"))
            current_trend = str(signal_row.get("trend", ""))

            if trade.side == "BULLISH_ENTRY" and (current_signal == "BEARISH_ENTRY" or current_trend == "bearish"):
                return True, "Trend/signal flipped bearish"

            if trade.side == "BEARISH_ENTRY" and (current_signal == "BULLISH_ENTRY" or current_trend == "bullish"):
                return True, "Trend/signal flipped bullish"

        return False, "Hold"

    def manage_open_trade(self, signal_row: Optional[pd.Series] = None) -> Optional[ExecutionResult]:
        trade = self.state.active_trade
        if trade is None or trade.status != "OPEN":
            return None

        if self.cfg.paper_trade:
            # Paper: _should_exit uses _paper_current_credit for all exit decisions
            should_exit, reason = self._should_exit(signal_row)
            if should_exit:
                return self._paper_exit(reason, signal_row)
            return None

        # Live: fetch real LTPs and make all credit-based exit decisions here
        try:
            ltp_data = self.tsl.get_ltp_data(names=[trade.short_leg_symbol, trade.hedge_leg_symbol])
            if ltp_data:
                short_ltp = float(ltp_data.get(trade.short_leg_symbol, 0))
                hedge_ltp = float(ltp_data.get(trade.hedge_leg_symbol, 0))

                if short_ltp > 0 and hedge_ltp > 0:
                    entry_credit = float(trade.entry_price)
                    current_credit = short_ltp - hedge_ltp

                    # Trail to breakeven
                    if not trade.sl_moved_to_breakeven:
                        if current_credit <= entry_credit * (1.0 - self.cfg.trail_to_breakeven_pct):
                            trade.sl_moved_to_breakeven = True
                            self.logger.info(f"Trailing Stop Triggered: SL moved to breakeven ({entry_credit})")

                    # Update PnL
                    trade.pnl = round((entry_credit - current_credit) * float(trade.qty), 2)
                    self.state.last_pnl = trade.pnl
                    self._save_state()

                    # Credit-based exits
                    target_value = entry_credit * (1.0 - self.cfg.target_profit_pct)
                    stop_value = entry_credit if trade.sl_moved_to_breakeven else entry_credit * (1.0 + self.cfg.stop_loss_pct)

                    if current_credit <= target_value:
                        return self._live_exit("Target reached", signal_row)
                    if current_credit >= stop_value:
                        return self._live_exit("Stop loss hit", signal_row)

        except Exception as e:
            self.logger.warning(f"Failed to fetch live LTPs for management: {e}")

        # Signal/trend flip check (applies to live regardless of LTP fetch result)
        if self.cfg.exit_on_opposite_signal and signal_row is not None:
            current_signal = str(signal_row.get("signal", "NO_TRADE"))
            current_trend = str(signal_row.get("trend", ""))
            if trade.side == "BULLISH_ENTRY" and (current_signal == "BEARISH_ENTRY" or current_trend == "bearish"):
                return self._live_exit("Trend/signal flipped bearish", signal_row)
            if trade.side == "BEARISH_ENTRY" and (current_signal == "BULLISH_ENTRY" or current_trend == "bullish"):
                return self._live_exit("Trend/signal flipped bullish", signal_row)

        return None

    # -----------------------------------------------------
    # PAPER EXECUTION
    # -----------------------------------------------------
    def _paper_enter(self, plan: Dict[str, Any], signal_row: pd.Series) -> ExecutionResult:
        valid, reason = self.risk.validate_spread(
            short_strike=plan["short"]["strike"],
            hedge_strike=plan["hedge"]["strike"],
            net_credit=plan["net_credit"],
            lot_size=plan["lot_size"],
        )
        if not valid:
            return ExecutionResult(ok=False, message=f"Spread validation failed: {reason}", payload=plan)

        qty_total, estimated_margin = self._estimate_qty_and_risk(plan)

        if qty_total <= 0:
            return ExecutionResult(ok=False, message="Computed quantity is zero", payload=plan)

        available_margin = self._safe_balance()
        can_take, reason = self.risk.can_take_new_trade(
            state=self.state,
            available_margin=available_margin,
            estimated_margin_required=estimated_margin,
        )
        if not can_take:
            return ExecutionResult(ok=False, message=reason, payload=plan)

        entry_credit = float(plan["net_credit"])
        entry_time = str(signal_row["timestamp"])
        entry_spot = float(signal_row["close"])

        trade = ActiveTrade(
            symbol=plan["underlying"],
            side=plan["signal"],
            entry_time=entry_time,
            entry_price=entry_credit,
            entry_spot=entry_spot,
            qty=qty_total,
            short_leg_symbol=plan["short"]["symbol"],
            hedge_leg_symbol=plan["hedge"]["symbol"],
            status="OPEN",
            pnl=0.0,
            remarks=f"PAPER | {plan['option_side']} spread",
        )

        self._set_in_trade(trade)

        self._append_trade_book({
            "event": "OPEN",
            "mode": "PAPER",
            "timestamp": entry_time,
            "symbol": trade.symbol,
            "signal": trade.side,
            "short_symbol": trade.short_leg_symbol,
            "hedge_symbol": trade.hedge_leg_symbol,
            "qty": trade.qty,
            "entry_price": trade.entry_price,
            "entry_spot": entry_spot,
            "available_margin": available_margin,
            "estimated_margin": estimated_margin,
            "remarks": trade.remarks,
        })

        self._safe_send(
            f"🟢 PAPER ENTRY\n"
            f"Signal: {trade.side}\n"
            f"Symbol: {trade.symbol}\n"
            f"Short: {trade.short_leg_symbol}\n"
            f"Hedge: {trade.hedge_leg_symbol}\n"
            f"Qty: {trade.qty}\n"
            f"Credit: {trade.entry_price:.2f}"
        )

        self.logger.info(
            f"[PAPER ENTRY] {trade.side} | {trade.short_leg_symbol} / {trade.hedge_leg_symbol} | qty={trade.qty} | credit={entry_credit:.2f}"
        )

        return ExecutionResult(ok=True, message="Paper trade opened", trade=trade, payload=plan)

    def _paper_exit(self, reason: str, signal_row: Optional[pd.Series] = None) -> ExecutionResult:
        trade = self.state.active_trade
        if trade is None:
            return ExecutionResult(ok=False, message="No active trade")

        exit_time = str(signal_row["timestamp"]) if signal_row is not None and "timestamp" in signal_row else datetime.now().isoformat()
        current_spot = float(signal_row["close"]) if signal_row is not None and "close" in signal_row else float(trade.entry_spot)
        current_credit = self._paper_current_credit(trade, current_spot)
        entry_credit = float(trade.entry_price)
        pnl = (entry_credit - current_credit) * float(trade.qty)

        trade.exit_time = exit_time
        trade.exit_price = current_credit
        trade.pnl = round(pnl, 2)
        trade.status = "CLOSED"
        trade.remarks = f"{trade.remarks} | EXIT: {reason}"

        self._append_trade_book({
            "event": "CLOSE",
            "mode": "PAPER",
            "timestamp": exit_time,
            "symbol": trade.symbol,
            "signal": trade.side,
            "short_symbol": trade.short_leg_symbol,
            "hedge_symbol": trade.hedge_leg_symbol,
            "qty": trade.qty,
            "entry_price": trade.entry_price,
            "exit_price": trade.exit_price,
            "pnl": trade.pnl,
            "reason": reason,
            "remarks": trade.remarks,
        })

        self._safe_send(
            f"🔴 PAPER EXIT\n"
            f"Symbol: {trade.symbol}\n"
            f"Reason: {reason}\n"
            f"PnL: {trade.pnl:.2f}"
        )

        self.logger.info(f"[PAPER EXIT] {reason} | pnl={trade.pnl:.2f}")

        self._clear_active_trade()
        self._set_cooldown()

        return ExecutionResult(ok=True, message=reason, trade=trade)

    # -----------------------------------------------------
    # LIVE EXECUTION
    # -----------------------------------------------------
    def _wait_for_order_fill(self, order_id: str) -> Tuple[str, float]:
        start = datetime.now()

        while (datetime.now() - start).total_seconds() < self.cfg.max_wait_for_fill_seconds:
            status = self.tsl.get_order_status(order_id)
            if isinstance(status, dict):
                raise RuntimeError(f"Could not fetch order status for {order_id}: {status}")

            status = str(status).upper()

            if status in {"TRADED", "FILLED", "COMPLETE", "EXECUTED"}:
                price = float(self.tsl.get_executed_price(order_id))
                return status, price

            if status in {"REJECTED", "CANCELLED", "FAILED"}:
                raise RuntimeError(f"Order {order_id} ended with status: {status}")

        raise TimeoutError(f"Order {order_id} did not fill in time")

    def _place_live_leg(self, tradingsymbol: str, transaction_type: str, quantity: int, price: float = 0.0) -> Tuple[str, float]:
        last_err = None

        for attempt in range(1, self.cfg.max_order_retries + 1):
            try:
                order_id = self.tsl.place_order(
                    tradingsymbol=tradingsymbol,
                    exchange="NFO",
                    transaction_type=transaction_type,
                    quantity=int(quantity),
                    order_type=self.cfg.order_type,
                    trade_type=self.cfg.trade_product,
                    price=price,
                    trigger_price=0,
                    disclosed_quantity=0,
                    after_market_order=False,
                    validity=self.cfg.validity,
                    amo_time="OPEN",
                    bo_profit_value=None,
                    bo_stop_loss_value=None,
                    tag="Algo_DOS",
                    should_slice=False,
                )

                if not order_id:
                    raise RuntimeError("place_order returned empty order id")

                _, price = self._wait_for_order_fill(str(order_id))
                return str(order_id), float(price)

            except Exception as e:
                last_err = e
                self.logger.exception(f"Live leg attempt {attempt} failed for {tradingsymbol}: {e}")

        raise RuntimeError(f"Failed to place live leg for {tradingsymbol}: {last_err}")

    def _live_enter(self, plan: Dict[str, Any], signal_row: pd.Series) -> ExecutionResult:
        valid, reason = self.risk.validate_spread(
            short_strike=plan["short"]["strike"],
            hedge_strike=plan["hedge"]["strike"],
            net_credit=plan["net_credit"],
            lot_size=plan["lot_size"],
        )
        if not valid:
            return ExecutionResult(ok=False, message=f"Spread validation failed: {reason}", payload=plan)

        qty_total, estimated_margin = self._estimate_qty_and_risk(plan)

        if qty_total <= 0:
            return ExecutionResult(ok=False, message="Computed quantity is zero", payload=plan)

        available_margin = self._safe_balance()
        can_take, reason = self.risk.can_take_new_trade(
            state=self.state,
            available_margin=available_margin,
            estimated_margin_required=estimated_margin,
        )
        if not can_take:
            return ExecutionResult(ok=False, message=reason, payload=plan)

        # Fetch LTPs for limit calculation
        ltps = {}
        try:
            ltp_data = self.tsl.get_ltp_data(names=[plan["hedge"]["symbol"], plan["short"]["symbol"]])
            if ltp_data:
                ltps = ltp_data
        except Exception as e:
            self.logger.warning(f"Failed to fetch LTPs for limit calc: {e}")

        # Hedge Leg (BUY)
        hedge_ltp = float(ltps.get(plan["hedge"]["symbol"], plan["hedge"]["ltp"]))
        hedge_buffer = max(hedge_ltp * self.cfg.limit_buffer_pct, self.cfg.min_absolute_buffer)
        hedge_limit = round((hedge_ltp + hedge_buffer) * 20) / 20

        hedge_order_id, hedge_fill = self._place_live_leg(
            tradingsymbol=plan["hedge"]["symbol"],
            transaction_type="BUY",
            quantity=qty_total,
            price=hedge_limit,
        )

        try:
            # Short Leg (SELL)
            short_ltp = float(ltps.get(plan["short"]["symbol"], plan["short"]["ltp"]))
            short_buffer = max(short_ltp * self.cfg.limit_buffer_pct, self.cfg.min_absolute_buffer)
            short_limit = round(max(short_ltp - short_buffer, 0.05) * 20) / 20

            short_order_id, short_fill = self._place_live_leg(
                tradingsymbol=plan["short"]["symbol"],
                transaction_type="SELL",
                quantity=qty_total,
                price=short_limit,
            )
        except Exception:
            try:
                # Flatten the hedge leg if short leg fails
                flatten_limit = round(max(hedge_fill - hedge_buffer, 0.05) * 20) / 20
                self.tsl.place_order(
                    tradingsymbol=plan["hedge"]["symbol"],
                    exchange="NFO",
                    transaction_type="SELL",
                    quantity=qty_total,
                    order_type=self.cfg.order_type,
                    trade_type=self.cfg.trade_product,
                    price=flatten_limit,
                    trigger_price=0,
                    disclosed_quantity=0,
                    after_market_order=False,
                    validity=self.cfg.validity,
                    amo_time="OPEN",
                    tag="Algo_DOS_FLATTEN",
                    should_slice=False,
                )
            except Exception:
                pass
            raise

        entry_credit = float(short_fill - hedge_fill)
        entry_time = str(signal_row["timestamp"])
        entry_spot = float(signal_row["close"])

        trade = ActiveTrade(
            symbol=plan["underlying"],
            side=plan["signal"],
            entry_time=entry_time,
            entry_price=entry_credit,
            entry_spot=entry_spot,
            qty=qty_total,
            order_id=short_order_id,
            hedge_order_id=hedge_order_id,
            short_leg_symbol=plan["short"]["symbol"],
            hedge_leg_symbol=plan["hedge"]["symbol"],
            status="OPEN",
            pnl=0.0,
            remarks=f"LIVE | {plan['option_side']} spread",
        )

        self._set_in_trade(trade)

        self._append_trade_book({
            "event": "OPEN",
            "mode": "LIVE",
            "timestamp": entry_time,
            "symbol": trade.symbol,
            "signal": trade.side,
            "short_symbol": trade.short_leg_symbol,
            "hedge_symbol": trade.hedge_leg_symbol,
            "qty": trade.qty,
            "entry_price": trade.entry_price,
            "entry_spot": entry_spot,
            "short_order_id": trade.order_id,
            "hedge_order_id": trade.hedge_order_id,
            "remarks": trade.remarks,
        })

        self._safe_send(
            f"🟢 LIVE ENTRY\n"
            f"Signal: {trade.side}\n"
            f"Symbol: {trade.symbol}\n"
            f"Short: {trade.short_leg_symbol}\n"
            f"Hedge: {trade.hedge_leg_symbol}\n"
            f"Qty: {trade.qty}\n"
            f"Credit: {trade.entry_price:.2f}\n"
            f"OrderID: {trade.order_id}"
        )

        self.logger.info(
            f"[LIVE ENTRY] {trade.side} | {trade.short_leg_symbol} / {trade.hedge_leg_symbol} | qty={trade.qty} | credit={entry_credit:.2f}"
        )

        return ExecutionResult(ok=True, message="Live trade opened", trade=trade, payload=plan)

    def _live_exit(self, reason: str, signal_row: Optional[pd.Series] = None) -> ExecutionResult:
        trade = self.state.active_trade
        if trade is None:
            return ExecutionResult(ok=False, message="No active trade")

        qty = int(trade.qty)
        exit_time = str(signal_row["timestamp"]) if signal_row is not None and "timestamp" in signal_row else datetime.now().isoformat()

        ltps = {}
        try:
            ltp_data = self.tsl.get_ltp_data(names=[trade.short_leg_symbol, trade.hedge_leg_symbol])
            if ltp_data:
                ltps = ltp_data
        except Exception as e:
            self.logger.warning(f"Failed to fetch LTPs for live exit limit calc: {e}")

        # Short leg was SELL, so exit is BUY
        short_ltp = float(ltps.get(trade.short_leg_symbol, getattr(trade, "entry_price", 0)))
        short_buffer = max(short_ltp * self.cfg.limit_buffer_pct, self.cfg.min_absolute_buffer)
        short_limit = round((short_ltp + short_buffer) * 20) / 20 if short_ltp > 0 else 0.0

        short_close_id, short_close_px = self._place_live_leg(
            tradingsymbol=trade.short_leg_symbol,
            transaction_type="BUY",
            quantity=qty,
            price=short_limit,
        )

        # Hedge leg was BUY, so exit is SELL
        hedge_ltp = float(ltps.get(trade.hedge_leg_symbol, getattr(trade, "entry_price", 0)))
        hedge_buffer = max(hedge_ltp * self.cfg.limit_buffer_pct, self.cfg.min_absolute_buffer)
        hedge_limit = round(max(hedge_ltp - hedge_buffer, 0.05) * 20) / 20 if hedge_ltp > 0 else 0.0

        hedge_close_id, hedge_close_px = self._place_live_leg(
            tradingsymbol=trade.hedge_leg_symbol,
            transaction_type="SELL",
            quantity=qty,
            price=hedge_limit,
        )

        entry_credit = float(trade.entry_price)
        current_credit = float(short_close_px - hedge_close_px)
        pnl = (entry_credit - current_credit) * float(qty)

        trade.exit_time = exit_time
        trade.exit_price = current_credit
        trade.pnl = round(pnl, 2)
        trade.status = "CLOSED"
        trade.remarks = f"{trade.remarks} | EXIT: {reason}"
        trade.order_id = f"{trade.order_id}|{short_close_id}"
        trade.hedge_order_id = f"{trade.hedge_order_id}|{hedge_close_id}"

        self._append_trade_book({
            "event": "CLOSE",
            "mode": "LIVE",
            "timestamp": exit_time,
            "symbol": trade.symbol,
            "signal": trade.side,
            "short_symbol": trade.short_leg_symbol,
            "hedge_symbol": trade.hedge_leg_symbol,
            "qty": trade.qty,
            "entry_price": trade.entry_price,
            "exit_price": trade.exit_price,
            "pnl": trade.pnl,
            "reason": reason,
            "short_close_id": short_close_id,
            "hedge_close_id": hedge_close_id,
            "remarks": trade.remarks,
        })

        self._safe_send(
            f"🔴 LIVE EXIT\n"
            f"Symbol: {trade.symbol}\n"
            f"Reason: {reason}\n"
            f"PnL: {trade.pnl:.2f}"
        )

        self.logger.info(f"[LIVE EXIT] {reason} | pnl={trade.pnl:.2f}")

        self._clear_active_trade()
        self._set_cooldown()

        return ExecutionResult(ok=True, message=reason, trade=trade)

    # -----------------------------------------------------
    # PUBLIC API
    # -----------------------------------------------------
    def on_signal(self, signal_row: pd.Series) -> ExecutionResult:
        signal = str(signal_row.get("signal", "NO_TRADE"))

        if self.state.active_trade is not None:
            should_exit, reason = self._should_exit(signal_row)
            if should_exit:
                if self.cfg.paper_trade:
                    return self._paper_exit(reason, signal_row)
                return self._live_exit(reason, signal_row)

            self.mark_to_market(signal_row)
            return ExecutionResult(ok=True, message="Trade held", trade=self.state.active_trade)

        if signal not in {"BULLISH_ENTRY", "BEARISH_ENTRY"}:
            return ExecutionResult(ok=False, message="No entry signal")

        try:
            plan = self.build_spread_plan(signal_row)

            if self.cfg.paper_trade:
                return self._paper_enter(plan, signal_row)

            return self._live_enter(plan, signal_row)

        except Exception as e:
            self.logger.exception(f"Signal execution failed: {e}")
            self._safe_send(f"⚠️ EXECUTION ERROR\n{e}")
            return ExecutionResult(ok=False, message=str(e))

    def flatten(self, reason: str = "Manual flatten") -> ExecutionResult:
        if self.state.active_trade is None:
            return ExecutionResult(ok=False, message="No active trade to flatten")

        if self.cfg.paper_trade:
            return self._paper_exit(reason, None)

        return self._live_exit(reason, None)

    def pause(self) -> None:
        self.state.engine_enabled = False
        self.state.trade_mode = "DISABLED"
        self._save_state()

    def resume(self) -> None:
        self.state.engine_enabled = True
        self.state.trade_mode = "WAITING"
        self._save_state()