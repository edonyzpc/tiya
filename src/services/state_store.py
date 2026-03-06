import asyncio
import copy
import json
from pathlib import Path
from typing import Any, Optional, cast

from ..domain.models import AgentProvider

SCHEMA_VERSION = 2
_PROVIDERS: tuple[AgentProvider, AgentProvider] = ("codex", "claude")


class StateStore:
    def __init__(self, path: Path, default_provider: AgentProvider = "codex", flush_delay_sec: float = 1.0):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.default_provider = default_provider if default_provider in _PROVIDERS else "codex"
        self.flush_delay_sec = max(0.0, float(flush_delay_sec))
        self.data: dict[str, Any] = self._default_state()
        self._lock = asyncio.Lock()
        self._flush_task: Optional[asyncio.Task[None]] = None
        self._dirty = False
        self._load()

    @staticmethod
    def _default_state() -> dict[str, Any]:
        return {"schema_version": SCHEMA_VERSION, "users": {}}

    def _load(self) -> None:
        if self.path.exists():
            try:
                self.data = json.loads(self.path.read_text(encoding="utf-8"))
            except Exception:
                self.data = self._default_state()
        else:
            self.data = self._default_state()

        changed = self._normalize_state()
        if changed:
            self._atomic_write(self.data)

    def _normalize_state(self) -> bool:
        changed = False
        schema_version = self.data.get("schema_version")
        if schema_version != SCHEMA_VERSION:
            self.data["schema_version"] = SCHEMA_VERSION
            changed = True

        users = self.data.get("users")
        if not isinstance(users, dict):
            self.data["users"] = {}
            users = self.data["users"]
            changed = True

        for key, user_data in list(users.items()):
            if not isinstance(user_data, dict):
                users[key] = {}
                user_data = users[key]
                changed = True
            if self._normalize_user_data(user_data):
                changed = True
        return changed

    def _normalize_user_data(self, user_data: dict[str, Any]) -> bool:
        changed = False
        had_active_provider_key = "active_provider" in user_data

        providers = user_data.get("providers")
        if not isinstance(providers, dict):
            providers = {}
            user_data["providers"] = providers
            changed = True

        for provider in _PROVIDERS:
            bucket = providers.get(provider)
            if not isinstance(bucket, dict):
                providers[provider] = self._empty_provider_bucket()
                changed = True
                continue

            if bucket.get("active_session_id") is not None and not isinstance(bucket.get("active_session_id"), str):
                bucket["active_session_id"] = None
                changed = True
            if bucket.get("active_cwd") is not None and not isinstance(bucket.get("active_cwd"), str):
                bucket["active_cwd"] = None
                changed = True

            last_session_ids = bucket.get("last_session_ids")
            if not isinstance(last_session_ids, list):
                bucket["last_session_ids"] = []
                changed = True
            else:
                normalized_ids = [str(v) for v in last_session_ids if str(v).strip()]
                if normalized_ids != last_session_ids:
                    bucket["last_session_ids"] = normalized_ids
                    changed = True

            pending_pick = bucket.get("pending_session_pick")
            normalized_pending = bool(pending_pick)
            if pending_pick != normalized_pending:
                bucket["pending_session_pick"] = normalized_pending
                changed = True

        legacy_active_session = user_data.pop("active_session_id", None)
        legacy_active_cwd = user_data.pop("active_cwd", None)
        legacy_last_session_ids = user_data.pop("last_session_ids", None)
        legacy_pending = user_data.pop("pending_session_pick", None)
        if (
            legacy_active_session is not None
            or legacy_active_cwd is not None
            or legacy_last_session_ids is not None
            or legacy_pending is not None
        ):
            changed = True
            codex_bucket = cast(dict[str, Any], providers["codex"])
            if codex_bucket.get("active_session_id") is None and isinstance(legacy_active_session, str):
                codex_bucket["active_session_id"] = legacy_active_session
            if codex_bucket.get("active_cwd") is None and isinstance(legacy_active_cwd, str):
                codex_bucket["active_cwd"] = legacy_active_cwd
            if not codex_bucket.get("last_session_ids") and isinstance(legacy_last_session_ids, list):
                codex_bucket["last_session_ids"] = [str(v) for v in legacy_last_session_ids if str(v).strip()]
            if not codex_bucket.get("pending_session_pick") and legacy_pending is not None:
                codex_bucket["pending_session_pick"] = bool(legacy_pending)

        active_provider = user_data.get("active_provider")
        if not isinstance(active_provider, str) or active_provider not in _PROVIDERS:
            if not had_active_provider_key:
                codex_active = providers["codex"].get("active_session_id")
                user_data["active_provider"] = "codex" if isinstance(codex_active, str) and codex_active else self.default_provider
            else:
                user_data["active_provider"] = self.default_provider
            changed = True

        return changed

    @staticmethod
    def _empty_provider_bucket() -> dict[str, Any]:
        return {
            "active_session_id": None,
            "active_cwd": None,
            "last_session_ids": [],
            "pending_session_pick": False,
        }

    def _atomic_write(self, payload: dict[str, Any]) -> None:
        tmp_path = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp_path.replace(self.path)

    def _get_user_unlocked(self, user_id: int) -> dict[str, Any]:
        users = self.data.setdefault("users", {})
        if not isinstance(users, dict):
            users = {}
            self.data["users"] = users
        key = str(user_id)
        if key not in users or not isinstance(users[key], dict):
            users[key] = {}
        user_data = cast(dict[str, Any], users[key])
        self._normalize_user_data(user_data)
        return user_data

    def _resolve_provider_unlocked(self, user_id: int, provider: Optional[AgentProvider]) -> AgentProvider:
        if provider in _PROVIDERS:
            return provider
        active_provider = self._get_user_unlocked(user_id).get("active_provider")
        if isinstance(active_provider, str) and active_provider in _PROVIDERS:
            return cast(AgentProvider, active_provider)
        return self.default_provider

    def _provider_bucket(self, user_data: dict[str, Any], provider: AgentProvider) -> dict[str, Any]:
        providers = user_data.setdefault("providers", {})
        if not isinstance(providers, dict):
            providers = {}
            user_data["providers"] = providers
        bucket = providers.get(provider)
        if not isinstance(bucket, dict):
            bucket = self._empty_provider_bucket()
            providers[provider] = bucket
        return cast(dict[str, Any], bucket)

    def _mark_dirty_unlocked(self) -> None:
        self._dirty = True
        if self._flush_task is None or self._flush_task.done():
            self._flush_task = asyncio.create_task(self._flush_after_delay())

    async def _flush_after_delay(self) -> None:
        try:
            await asyncio.sleep(self.flush_delay_sec)
            await self.save()
        except asyncio.CancelledError:
            return

    async def save(self) -> None:
        async with self._lock:
            if not self._dirty:
                return
            payload = copy.deepcopy(self.data)
            self._dirty = False
        await asyncio.to_thread(self._atomic_write, payload)

    async def close(self) -> None:
        flush_task: Optional[asyncio.Task[None]]
        async with self._lock:
            flush_task = self._flush_task
            self._flush_task = None
        if flush_task is not None and flush_task is not asyncio.current_task():
            flush_task.cancel()
            try:
                await flush_task
            except asyncio.CancelledError:
                pass
        await self.save()

    async def set_active_provider(self, user_id: int, provider: AgentProvider) -> None:
        async with self._lock:
            user_data = self._get_user_unlocked(user_id)
            user_data["active_provider"] = provider
            self._mark_dirty_unlocked()

    async def get_active_provider(self, user_id: int) -> AgentProvider:
        async with self._lock:
            user_data = self._get_user_unlocked(user_id)
            active_provider = user_data.get("active_provider")
            if isinstance(active_provider, str) and active_provider in _PROVIDERS:
                return cast(AgentProvider, active_provider)
            return self.default_provider

    async def set_active_session(
        self,
        user_id: int,
        session_id: str,
        cwd: str,
        provider: Optional[AgentProvider] = None,
    ) -> None:
        async with self._lock:
            user_data = self._get_user_unlocked(user_id)
            resolved_provider = self._resolve_provider_unlocked(user_id, provider)
            bucket = self._provider_bucket(user_data, resolved_provider)
            bucket["active_session_id"] = session_id
            bucket["active_cwd"] = cwd
            self._mark_dirty_unlocked()

    async def clear_active_session(
        self,
        user_id: int,
        cwd: str,
        provider: Optional[AgentProvider] = None,
    ) -> None:
        async with self._lock:
            user_data = self._get_user_unlocked(user_id)
            resolved_provider = self._resolve_provider_unlocked(user_id, provider)
            bucket = self._provider_bucket(user_data, resolved_provider)
            bucket["active_session_id"] = None
            bucket["active_cwd"] = cwd
            self._mark_dirty_unlocked()

    async def get_active(
        self,
        user_id: int,
        provider: Optional[AgentProvider] = None,
    ) -> tuple[Optional[str], Optional[str]]:
        async with self._lock:
            user_data = self._get_user_unlocked(user_id)
            resolved_provider = self._resolve_provider_unlocked(user_id, provider)
            bucket = self._provider_bucket(user_data, resolved_provider)
            session_id = bucket.get("active_session_id")
            active_cwd = bucket.get("active_cwd")
            return (
                session_id if isinstance(session_id, str) else None,
                active_cwd if isinstance(active_cwd, str) else None,
            )

    async def set_last_session_ids(
        self,
        user_id: int,
        session_ids: list[str],
        provider: Optional[AgentProvider] = None,
    ) -> None:
        async with self._lock:
            user_data = self._get_user_unlocked(user_id)
            resolved_provider = self._resolve_provider_unlocked(user_id, provider)
            bucket = self._provider_bucket(user_data, resolved_provider)
            bucket["last_session_ids"] = [str(v) for v in session_ids if str(v).strip()]
            self._mark_dirty_unlocked()

    async def get_last_session_ids(self, user_id: int, provider: Optional[AgentProvider] = None) -> list[str]:
        async with self._lock:
            user_data = self._get_user_unlocked(user_id)
            resolved_provider = self._resolve_provider_unlocked(user_id, provider)
            bucket = self._provider_bucket(user_data, resolved_provider)
            values = bucket.get("last_session_ids")
            if not isinstance(values, list):
                return []
            return [str(v) for v in values]

    async def set_pending_session_pick(
        self,
        user_id: int,
        enabled: bool,
        provider: Optional[AgentProvider] = None,
    ) -> None:
        async with self._lock:
            user_data = self._get_user_unlocked(user_id)
            resolved_provider = self._resolve_provider_unlocked(user_id, provider)
            bucket = self._provider_bucket(user_data, resolved_provider)
            bucket["pending_session_pick"] = bool(enabled)
            self._mark_dirty_unlocked()

    async def is_pending_session_pick(self, user_id: int, provider: Optional[AgentProvider] = None) -> bool:
        async with self._lock:
            user_data = self._get_user_unlocked(user_id)
            resolved_provider = self._resolve_provider_unlocked(user_id, provider)
            bucket = self._provider_bucket(user_data, resolved_provider)
            return bool(bucket.get("pending_session_pick"))
