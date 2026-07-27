import os
from pathlib import Path
import requests
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")

class TelegramNotifier:
    def __init__(self):
        self.token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        self.chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
        self.is_configured = self._check_config()

    def _check_config(self) -> bool:
        if not self.token or not self.chat_id:
            return False
        placeholders = ("your_telegram", "your", "replace_me", "placeholder", "changeme")
        if any(p in f"{self.token}:{self.chat_id}".lower() for p in placeholders):
            return False
        return True

    def send(self, message: str) -> None:
        if not self.is_configured:
            return
        
        try:
            requests.post(
                f"https://api.telegram.org/bot{self.token}/sendMessage",
                json={"chat_id": self.chat_id, "text": message},
                timeout=10,
            )
        except Exception:
            pass

# Create a singleton instance to be used across the app
notifier = TelegramNotifier()
