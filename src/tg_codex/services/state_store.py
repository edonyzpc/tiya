import json
import threading
from pathlib import Path
from typing import Any, Optional


class StateStore:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.data: dict[str, Any] = {"users": {}}
        self._lock = threading.Lock()
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            self.data = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            self.data = {"users": {}}

    def _atomic_write(self, payload: dict[str, Any]) -> None:
        tmp_path = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp_path.replace(self.path)

    def save(self) -> None:
        with self._lock:
            self._atomic_write(self.data)

    def get_user(self, user_id: int) -> dict[str, Any]:
        users = self.data.setdefault("users", {})
        key = str(user_id)
        if key not in users:
            users[key] = {}
        return users[key]

    def set_active_session(self, user_id: int, session_id: str, cwd: str) -> None:
        with self._lock:
            user_data = self.get_user(user_id)
            user_data["active_session_id"] = session_id
            user_data["active_cwd"] = cwd
            self._atomic_write(self.data)

    def clear_active_session(self, user_id: int, cwd: str) -> None:
        with self._lock:
            user_data = self.get_user(user_id)
            user_data["active_session_id"] = None
            user_data["active_cwd"] = cwd
            self._atomic_write(self.data)

    def get_active(self, user_id: int) -> tuple[Optional[str], Optional[str]]:
        user_data = self.get_user(user_id)
        return user_data.get("active_session_id"), user_data.get("active_cwd")

    def set_last_session_ids(self, user_id: int, session_ids: list[str]) -> None:
        with self._lock:
            user_data = self.get_user(user_id)
            user_data["last_session_ids"] = session_ids
            self._atomic_write(self.data)

    def get_last_session_ids(self, user_id: int) -> list[str]:
        user_data = self.get_user(user_id)
        values = user_data.get("last_session_ids")
        if not isinstance(values, list):
            return []
        return [str(v) for v in values]

    def set_pending_session_pick(self, user_id: int, enabled: bool) -> None:
        with self._lock:
            user_data = self.get_user(user_id)
            user_data["pending_session_pick"] = bool(enabled)
            self._atomic_write(self.data)

    def is_pending_session_pick(self, user_id: int) -> bool:
        user_data = self.get_user(user_id)
        return bool(user_data.get("pending_session_pick"))
