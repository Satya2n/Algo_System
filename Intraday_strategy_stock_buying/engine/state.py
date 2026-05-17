# engine/state.py
import json
import os
from dataclasses import dataclass, asdict
from typing import Dict, Optional
import datetime

@dataclass
class ActiveTrade:
    symbol: str
    side: str  # "BUY" or "SELL"
    qty: int
    entry_price: float
    entry_order_id: str
    sl_order_id: str
    stop_loss: float
    tp1: float
    partial_done: bool = False
    remaining_qty: int = 0
    trail_sl: float = 0.0
    pnl: float = 0.0
    pending_sl_update: bool = False
    pending_sl_price: float = 0.0

@dataclass
class EngineState:
    date: str
    trade_count_today: int
    active_trades: Dict[str, ActiveTrade]
    realized_daily_pnl: float = 0.0

class StateManager:
    def __init__(self, state_file: str = "state/engine_state.json"):
        self.state_file = state_file
        os.makedirs(os.path.dirname(self.state_file), exist_ok=True)
        self.state = self.load_state()

    def load_state(self) -> EngineState:
        today = str(datetime.datetime.now().date())
        if os.path.exists(self.state_file):
            try:
                with open(self.state_file, 'r') as f:
                    data = json.load(f)
                    
                # Reset if it's a new day
                if data.get("date") != today:
                    return EngineState(date=today, trade_count_today=0, active_trades={}, realized_daily_pnl=0.0)

                active_trades = {
                    k: ActiveTrade(**v) for k, v in data.get("active_trades", {}).items()
                }
                return EngineState(
                    date=data.get("date", today),
                    trade_count_today=data.get("trade_count_today", 0),
                    active_trades=active_trades,
                    realized_daily_pnl=data.get("realized_daily_pnl", 0.0)
                )
            except Exception as e:
                import logging
                logging.getLogger("State").error(f"State load failed, starting fresh: {e}")

        return EngineState(date=today, trade_count_today=0, active_trades={}, realized_daily_pnl=0.0)

    def save_state(self):
        data = {
            "date": self.state.date,
            "trade_count_today": self.state.trade_count_today,
            "realized_daily_pnl": self.state.realized_daily_pnl,
            "active_trades": {k: asdict(v) for k, v in self.state.active_trades.items()}
        }
        tmp_path = self.state_file + ".tmp"
        with open(tmp_path, 'w') as f:
            json.dump(data, f, indent=4)
        os.replace(tmp_path, self.state_file)

    def add_trade(self, trade: ActiveTrade):
        self.state.active_trades[trade.symbol] = trade
        self.state.trade_count_today += 1
        self.save_state()

    def remove_trade(self, symbol: str):
        if symbol in self.state.active_trades:
            trade = self.state.active_trades[symbol]
            self.state.realized_daily_pnl += trade.pnl
            del self.state.active_trades[symbol]
            self.save_state()

    def update_trade(self, trade: ActiveTrade):
        self.state.active_trades[trade.symbol] = trade
        self.save_state()

    def get_trade(self, symbol: str) -> Optional[ActiveTrade]:
        return self.state.active_trades.get(symbol)
        
    def get_active_trade_count(self) -> int:
        return len(self.state.active_trades)
