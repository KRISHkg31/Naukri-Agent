"""Per-user folders for the multi-user Telegram bot: data/users/<telegram_chat_id>/."""
from __future__ import annotations

import json
import os
import shutil
from datetime import datetime
from pathlib import Path
from typing import Optional

import yaml

from naukri.store import EncryptedStore

# pending -> approved (no session yet) -> active; needs_login when the session expires
STATUSES = ("pending", "approved", "active", "needs_login", "paused")


class User:
    def __init__(self, root: Path, chat_id: int | str):
        self.id = str(chat_id)
        self.dir = root / self.id
        self.meta_path = self.dir / "meta.json"
        self.state_path = self.dir / "state.json"
        self.config_path = self.dir / "config.yaml"  # optional: rotate mode (owner's own lists)
        self.log_dir = self.dir / "logs"
        self.meta = json.loads(self.meta_path.read_text(encoding="utf-8")) if self.meta_path.exists() else {}

    @property
    def exists(self) -> bool:
        return self.meta_path.exists()

    @property
    def status(self) -> str:
        return self.meta.get("status", "")

    @property
    def name(self) -> str:
        return self.meta.get("name", self.id)

    def store(self, key: str) -> EncryptedStore:
        return EncryptedStore(self.dir / "session.enc", key)

    def rotate_cfg(self) -> Optional[dict]:
        if not self.config_path.exists():
            return None
        from naukri.rotation import validate_config

        cfg = yaml.safe_load(self.config_path.read_text(encoding="utf-8"))
        validate_config(cfg)
        return cfg

    def save(self, **changes) -> None:
        self.meta.update(changes)
        self.dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.dir, 0o700)
        self.meta_path.write_text(json.dumps(self.meta, indent=2), encoding="utf-8")

    def create(self, name: str, status: str) -> None:
        self.save(id=self.id, name=name, status=status, created=datetime.now().isoformat(timespec="seconds"))

    def delete(self) -> None:
        shutil.rmtree(self.dir, ignore_errors=True)


def all_users(root: Path) -> list[User]:
    if not root.exists():
        return []
    return [User(root, d.name) for d in sorted(root.iterdir()) if (d / "meta.json").exists()]
