# engine/telegram_control.py

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from config.credentials import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID


@dataclass
class TelegramConfig:
    bot_token: str = TELEGRAM_BOT_TOKEN
    chat_id: str = TELEGRAM_CHAT_ID
    enabled: bool = True


class TelegramNotifier:
    def __init__(self, cfg: TelegramConfig, logger: Optional[logging.Logger] = None):
        self.cfg = cfg
        self.logger = logger or logging.getLogger(__name__)

    def send(self, message: str) -> bool:
        """
        Send a Telegram message using the wrapper's logic later or direct HTTP call now.
        Returns True on success, False on failure.
        """
        if not self.cfg.enabled:
            return False

        if not self.cfg.bot_token or not self.cfg.chat_id:
            self.logger.warning("Telegram credentials missing")
            return False

        try:
            # Import here so trading engine still starts even if Telegram libraries are missing later
            import requests
            from urllib.parse import quote

            encoded = quote(message)
            url = (
                f"https://api.telegram.org/bot{self.cfg.bot_token}"
                f"/sendMessage?chat_id={self.cfg.chat_id}&text={encoded}"
            )
            resp = requests.get(url, timeout=10)
            resp.raise_for_status()
            return True
        except Exception as e:
            self.logger.exception(f"Telegram send failed: {e}")
            return False