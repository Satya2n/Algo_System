# engine/risk.py
import logging

class RiskManager:
    def __init__(self, cfg):
        self.cfg = cfg
        self.logger = logging.getLogger("RiskManager")
        # Snapshot of capital to ensure consistent sizing throughout the day
        self.daily_starting_capital = 0.0

    def set_daily_capital(self, capital: float):
        if self.daily_starting_capital == 0.0:
            self.daily_starting_capital = capital
            self.logger.info(f"Daily starting capital locked at ₹{capital:.2f}")

    def calculate_position_size(self, entry_price: float, stop_loss: float) -> tuple[int, float]:
        """
        qty is limited by:
        1) 1% risk per trade amount
        2) Max capital allowed for one slot
        """
        capital = self.daily_starting_capital
        if capital <= 0:
            return 0, 0.0

        risk_amount = capital * (self.cfg.MAX_RISK_PER_TRADE_PCT / 100.0)
        per_share_risk = abs(entry_price - stop_loss)

        if per_share_risk <= 0:
            return 0, 0.0

        qty_by_risk = int(risk_amount / per_share_risk)

        # Capital-slot cap
        deployable_capital = capital * self.cfg.CAPITAL_UTILIZATION
        capital_slot = deployable_capital / self.cfg.MAX_CONCURRENT_TRADES if self.cfg.MAX_CONCURRENT_TRADES > 0 else 0
        
        qty_by_capital = int(capital_slot / entry_price)

        qty = min(qty_by_risk, qty_by_capital)

        if qty < 1:
            qty = 0

        return qty, risk_amount
