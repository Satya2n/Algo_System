from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, date
from enum import Enum
from pathlib import Path
from typing import Any, Dict, Optional


class TradeMode(str, Enum):
    WAITING = "WAITING"
    IN_TRADE = "IN_TRADE"
    COOLDOWN = "COOLDOWN"
    DISABLED = "DISABLED"


@dataclass
class ActiveTrade:
    symbol: str = "NIFTY"
    side: str = ""
    entry_time: str = ""
    entry_price: float = 0.0
    entry_spot: float = 0.0
    exit_time: str = ""
    exit_price: float = 0.0
    qty: int = 0
    order_id: str = ""
    hedge_order_id: str = ""
    short_leg_symbol: str = ""
    hedge_leg_symbol: str = ""
    status: str = "OPEN"
    pnl: float = 0.0
    remarks: str = ""
    sl_moved_to_breakeven: bool = False


@dataclass
class EngineState:
    paper_trade: bool = True
    engine_enabled: bool = True
    trade_mode: str = TradeMode.WAITING.value
    trade_taken_today: bool = False
    today_trade_count: int = 0
    max_trades_per_day: int = 1
    last_processed_candle: str = ""
    last_pnl: float = 0.0
    last_update: str = field(default_factory=lambda: datetime.now().isoformat())
    active_trade: Optional[ActiveTrade] = None
    current_date: str = field(default_factory=lambda: date.today().isoformat())

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        if self.active_trade is not None:
            data["active_trade"] = asdict(self.active_trade)
        return data

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "EngineState":
        active_trade_data = data.get("active_trade")
        active_trade = ActiveTrade(**active_trade_data) if isinstance(active_trade_data, dict) else None

        return cls(
            paper_trade=bool(data.get("paper_trade", True)),
            engine_enabled=bool(data.get("engine_enabled", True)),
            trade_mode=str(data.get("trade_mode", TradeMode.WAITING.value)),
            trade_taken_today=bool(data.get("trade_taken_today", False)),
            today_trade_count=int(data.get("today_trade_count", 0)),
            max_trades_per_day=int(data.get("max_trades_per_day", 1)),
            last_processed_candle=str(data.get("last_processed_candle", "")),
            last_pnl=float(data.get("last_pnl", 0.0)),
            last_update=str(data.get("last_update", datetime.now().isoformat())),
            active_trade=active_trade,
            current_date=str(data.get("current_date", date.today().isoformat())),
        )


class StateStore:
    def __init__(self, path: str = "state/engine_state.json"):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def load(self) -> EngineState:
        if not self.path.exists():
            return EngineState()

        with self.path.open("r", encoding="utf-8") as f:
            raw = json.load(f)

        return EngineState.from_dict(raw)

    def save(self, state: EngineState) -> None:
        state.last_update = datetime.now().isoformat()
        tmp = self.path.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(state.to_dict(), f, indent=2, ensure_ascii=False)
        os.replace(tmp, self.path)

    def reset_daily_counters_if_new_day(self, state: EngineState) -> EngineState:
        today = date.today().isoformat()
        if state.current_date != today:
            state.current_date = today
            state.trade_taken_today = False
            state.today_trade_count = 0
            state.last_processed_candle = ""
        return state
