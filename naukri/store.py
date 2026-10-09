"""Where a user's Naukri browser session (cookies) is kept: plain JSON or encrypted."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken


class PlainStore:
    def __init__(self, path: Path):
        self.path = path

    def load(self) -> Optional[dict]:
        return json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else None

    def save(self, state: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        _write_private(self.path, json.dumps(state).encode())

    def __str__(self) -> str:
        return str(self.path)


class EncryptedStore(PlainStore):
    """Session encrypted with SESSION_KEY from .env (Fernet: AES-128 + HMAC)."""

    def __init__(self, path: Path, key: str):
        super().__init__(path)
        self.fernet = Fernet(key.encode())

    def load(self) -> Optional[dict]:
        if not self.path.exists():
            return None
        try:
            return json.loads(self.fernet.decrypt(self.path.read_bytes()))
        except InvalidToken:
            raise RuntimeError(f"Cannot decrypt {self.path} - SESSION_KEY changed?")

    def save(self, state: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        _write_private(self.path, self.fernet.encrypt(json.dumps(state).encode()))


def _write_private(path: Path, data: bytes) -> None:
    """Write a file readable only by the owner (0600 on Linux)."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    os.replace(tmp, path)


def new_key() -> str:
    return Fernet.generate_key().decode()
