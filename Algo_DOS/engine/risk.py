from __future__ import annotations

from dataclasses import dataclass


@dataclass
class RiskConfig:
    capital: float = 100000.0
    risk_per_trade_pct: float = 0.02
    max_trades_per_day: int = 1
    max_margin_utilization_pct: float = 0.30
    buffer_margin_pct: float = 0.10
    min_lot_size: int = 1
    max_spread_width: int = 300
    min_credit: float = 0.0
    max_slippage_pct: float = 0.15
    margin_per_lot: float = 45000.0


class RiskManager:
    def __init__(self, cfg: RiskConfig):
        self.cfg = cfg

    def get_max_risk_amount(self) -> float:
        return self.cfg.capital * self.cfg.risk_per_trade_pct

    def can_take_new_trade(self, state, available_margin: float, estimated_margin_required: float) -> tuple[bool, str]:
        if not state.engine_enabled:
            return False, "Engine disabled"

        if state.trade_mode == "IN_TRADE":
            return False, "Already in trade"

        if state.trade_taken_today and state.today_trade_count >= state.max_trades_per_day:
            return False, "Daily trade limit reached"

        if estimated_margin_required <= 0:
            return False, "Invalid estimated margin"

        max_allowed_margin = self.cfg.capital * self.cfg.max_margin_utilization_pct
        if estimated_margin_required > max_allowed_margin:
            return False, f"Margin too high: {estimated_margin_required:.2f} > {max_allowed_margin:.2f}"

        if available_margin < estimated_margin_required * (1 + self.cfg.buffer_margin_pct):
            return False, "Insufficient available margin"

        return True, "OK"

    def estimate_lots_by_risk(self, max_loss_per_lot: float, fallback_lots: int = 1) -> int:
        risk_amount = self.get_max_risk_amount()
        if max_loss_per_lot <= 0:
            return fallback_lots

        lots = int(risk_amount // max_loss_per_lot)
        return max(lots, fallback_lots)

    def validate_spread(self, short_strike: float, hedge_strike: float, net_credit: float, lot_size: int) -> tuple[bool, str]:
        if short_strike <= 0 or hedge_strike <= 0:
            return False, "Invalid strikes"

        width = abs(short_strike - hedge_strike)
        if width <= 0:
            return False, "Spread width is zero"

        if width > self.cfg.max_spread_width:
            return False, f"Spread width too large: {width}"

        if net_credit < self.cfg.min_credit:
            return False, f"Net credit too low: {net_credit}"

        if lot_size < self.cfg.min_lot_size:
            return False, "Lot size invalid"

        if hedge_strike == short_strike:
            return False, "Hedge and short strike cannot be same"

        return True, "OK"

    def estimate_max_loss_per_lot(self, spread_width: float, lot_size: int, net_credit: float) -> float:
        if spread_width <= 0 or lot_size <= 0:
            return 0.0
        return max((spread_width - net_credit) * lot_size, 0.0)

    def position_size_for_spread(self, spread_width: float, lot_size: int, net_credit: float) -> int:
        max_loss_per_lot = self.estimate_max_loss_per_lot(spread_width, lot_size, net_credit)
        return self.estimate_lots_by_risk(max_loss_per_lot=max_loss_per_lot, fallback_lots=1)

    def get_required_broker_margin(self, qty_lots: int) -> float:
        return float(qty_lots * self.cfg.margin_per_lot)
