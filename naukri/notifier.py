"""Telegram notifications, and asking you for an OTP when Naukri wants one."""
from __future__ import annotations

import logging
import re
import sys
import time
from pathlib import Path
from typing import Optional

import requests

log = logging.getLogger(__name__)


class Notifier:
    def __init__(self, token: Optional[str], chat_id: Optional[str]):
        self.token = token or None
        self.chat_id = chat_id or None

    @property
    def enabled(self) -> bool:
        return bool(self.token and self.chat_id)

    def _api(self, method: str, timeout: int = 20, **kwargs) -> dict:
        r = requests.post(f"https://api.telegram.org/bot{self.token}/{method}", timeout=timeout, **kwargs)
        r.raise_for_status()
        return r.json()

    def send(self, text: str, html: bool = False) -> None:
        log.info("Notify: %s", text.replace("\n", " | "))
        if not self.enabled:
            return
        data = {"chat_id": self.chat_id, "text": text[:4000]}
        if html:
            data["parse_mode"] = "HTML"
        try:
            self._api("sendMessage", data=data)
        except requests.RequestException as e:
            log.warning("Telegram send failed: %s", e)

    def send_photo(self, path: Path, caption: str = "") -> None:
        if not self.enabled or not path.exists():
            return
        try:
            with path.open("rb") as f:
                self._api("sendPhoto", data={"chat_id": self.chat_id, "caption": caption[:1000]}, files={"photo": f})
        except requests.RequestException as e:
            log.warning("Telegram photo failed: %s", e)

    def ask_otp(self, timeout_s: int = 600) -> str:
        """Ask for the OTP on Telegram (or the terminal) and wait for the reply."""
        if not self.enabled:
            if sys.stdin and sys.stdin.isatty():
                return input("Naukri OTP: ").strip()
            raise RuntimeError("Naukri asked for an OTP but Telegram is not configured")

        # Skip anything sent before this request.
        old = self._api("getUpdates", data={"offset": -1, "timeout": 0}).get("result", [])
        offset = old[-1]["update_id"] + 1 if old else None
        self.send(f"🔐 Naukri is asking for an OTP. Reply here with the code (waiting {timeout_s // 60} min).")

        deadline = time.time() + timeout_s
        while time.time() < deadline:
            data = {"timeout": 25}
            if offset is not None:
                data["offset"] = offset
            try:
                updates = self._api("getUpdates", timeout=35, data=data).get("result", [])
            except requests.RequestException as e:
                log.warning("Telegram poll failed: %s", e)
                time.sleep(5)
                continue
            for u in updates:
                offset = u["update_id"] + 1
                msg = u.get("message") or {}
                if str(msg.get("chat", {}).get("id")) != str(self.chat_id):
                    continue
                m = re.search(r"\b(\d{4,8})\b", msg.get("text", ""))
                if m:
                    self.send("👍 Got it, entering OTP…")
                    return m.group(1)
        raise TimeoutError("No OTP received on Telegram in time")

    def discover_chat_ids(self) -> list[tuple[str, str]]:
        """Chats that have messaged the bot - used to find your TELEGRAM_CHAT_ID."""
        updates = self._api("getUpdates", data={"timeout": 0}).get("result", [])
        seen = {}
        for u in updates:
            chat = (u.get("message") or {}).get("chat") or {}
            if chat:
                seen[str(chat["id"])] = chat.get("first_name") or chat.get("title") or ""
        return list(seen.items())
