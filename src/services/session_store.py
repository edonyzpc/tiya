from __future__ import annotations

import asyncio
import inspect
from pathlib import Path
from typing import Optional, Protocol

from ..domain.models import SessionMeta
from .storage import StorageManager, compact_message


class AsyncSessionStoreProtocol(Protocol):
    async def refresh_recent(self) -> None:
        ...

    async def refresh_session(self, session_id: str) -> None:
        ...

    async def list_recent(self, limit: int = 10) -> list[SessionMeta]:
        ...

    async def find_by_id(self, session_id: str) -> Optional[SessionMeta]:
        ...

    async def get_history(self, session_id: str, limit: int = 10) -> tuple[Optional[SessionMeta], list[tuple[str, str]]]:
        ...


class AsyncSessionStore:
    def __init__(self, inner: object):
        self.inner = inner

    async def _invoke(self, method_name: str, *args: object) -> object:
        method = getattr(self.inner, method_name)
        result = method(*args)
        if inspect.isawaitable(result):
            return await result
        return result

    async def refresh_recent(self) -> None:
        await self._invoke("refresh_recent")

    async def refresh_session(self, session_id: str) -> None:
        await self._invoke("refresh_session", session_id)

    async def list_recent(self, limit: int = 10) -> list[SessionMeta]:
        return list(await self._invoke("list_recent", limit))

    async def find_by_id(self, session_id: str) -> Optional[SessionMeta]:
        value = await self._invoke("find_by_id", session_id)
        return value if isinstance(value, SessionMeta) or value is None else None

    async def get_history(
        self,
        session_id: str,
        limit: int = 10,
    ) -> tuple[Optional[SessionMeta], list[tuple[str, str]]]:
        value = await self._invoke("get_history", session_id, limit)
        if not isinstance(value, tuple) or len(value) != 2:
            return None, []
        meta, messages = value
        return (meta if isinstance(meta, SessionMeta) or meta is None else None, list(messages))

    async def refresh_all(self) -> None:
        if hasattr(self.inner, "refresh_all"):
            await self._invoke("refresh_all")


class _SQLiteSessionStore:
    provider: str

    def __init__(self, root: Path, storage: StorageManager):
        self.root = root.expanduser().resolve()
        self.storage = storage

    async def refresh_all(self) -> None:
        await self.storage.sessions.refresh_session_root(self.provider, self.root)  # type: ignore[arg-type]

    async def refresh_recent(self) -> None:
        await self.refresh_all()

    async def refresh_session(self, session_id: str) -> None:
        await self.storage.sessions.refresh_session(self.provider, self.root, session_id)  # type: ignore[arg-type]

    async def list_recent(self, limit: int = 10) -> list[SessionMeta]:
        return await self.storage.sessions.list_recent_sessions(self.provider, self.root, limit)  # type: ignore[arg-type]

    async def find_by_id(self, session_id: str) -> Optional[SessionMeta]:
        return await self.storage.sessions.find_session(self.provider, self.root, session_id)  # type: ignore[arg-type]

    async def get_history(
        self,
        session_id: str,
        limit: int = 10,
    ) -> tuple[Optional[SessionMeta], list[tuple[str, str]]]:
        return await self.storage.sessions.get_session_history(self.provider, self.root, session_id, limit)  # type: ignore[arg-type]

    @staticmethod
    def compact_message(text: str, limit: int = 320) -> str:
        return compact_message(text, limit=limit)


class CodexSessionStore(_SQLiteSessionStore):
    provider = "codex"


class ClaudeSessionStore(_SQLiteSessionStore):
    provider = "claude"


# Backward-compatible alias for existing imports.
SessionStore = CodexSessionStore
