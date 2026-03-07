from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Optional, cast

from ..domain.models import ActiveRunState, AgentProvider, PendingImage, PendingInteraction
from .storage import StorageConfig, StorageManager

SCHEMA_VERSION = 4
_PROVIDERS: tuple[AgentProvider, AgentProvider] = ("codex", "claude")


class StateStore:
    def __init__(
        self,
        path_or_storage: Path | StorageManager,
        default_provider: AgentProvider = "codex",
        flush_delay_sec: float = 1.0,
        *,
        storage_path: Optional[Path] = None,
        instance_id: str = "default",
        attachments_root: Optional[Path] = None,
        session_roots: Optional[dict[AgentProvider, Path]] = None,
        config_snapshot: Optional[dict[str, object]] = None,
    ):
        self.default_provider = default_provider if default_provider in _PROVIDERS else "codex"
        self.flush_delay_sec = max(0.0, float(flush_delay_sec))
        self._storage_lock = asyncio.Lock()
        if isinstance(path_or_storage, StorageManager):
            self.storage = path_or_storage
            self.path = self.storage.db_path
            self._storage_config: Optional[StorageConfig] = None
            return

        legacy_state_path = path_or_storage.expanduser()
        db_path = (storage_path or legacy_state_path.with_suffix(".db")).expanduser()
        attachment_dir = attachments_root.expanduser() if attachments_root is not None else legacy_state_path.parent / "attachments"
        self.path = db_path
        self.storage: Optional[StorageManager] = None
        self._storage_config = StorageConfig(
            db_path=db_path,
            instance_id=instance_id,
            default_provider=self.default_provider,
            attachments_root=attachment_dir,
            session_roots=session_roots or {},
            config_snapshot=config_snapshot,
        )

    async def close(self) -> None:
        storage = self.storage
        if storage is None:
            return
        await storage.close()

    async def save(self) -> None:
        return None

    async def set_active_provider(self, user_id: int, provider: AgentProvider) -> None:
        storage = await self._get_storage()
        await storage.state.set_active_provider(user_id, provider)

    async def get_active_provider(self, user_id: int) -> AgentProvider:
        storage = await self._get_storage()
        provider = await storage.state.get_active_provider(user_id)
        return cast(AgentProvider, provider if provider in _PROVIDERS else self.default_provider)

    async def set_active_session(
        self,
        user_id: int,
        session_id: str,
        cwd: str,
        provider: Optional[AgentProvider] = None,
    ) -> None:
        storage = await self._get_storage()
        await storage.state.set_active_session(user_id, self._resolve_provider(provider), session_id, cwd)

    async def clear_active_session(
        self,
        user_id: int,
        cwd: str,
        provider: Optional[AgentProvider] = None,
    ) -> None:
        storage = await self._get_storage()
        await storage.state.clear_active_session(user_id, self._resolve_provider(provider), cwd)

    async def get_active(
        self,
        user_id: int,
        provider: Optional[AgentProvider] = None,
    ) -> tuple[Optional[str], Optional[str]]:
        storage = await self._get_storage()
        return await storage.state.get_active(user_id, self._resolve_provider(provider))

    async def set_last_session_ids(
        self,
        user_id: int,
        session_ids: list[str],
        provider: Optional[AgentProvider] = None,
    ) -> None:
        storage = await self._get_storage()
        await storage.state.set_last_session_ids(user_id, self._resolve_provider(provider), session_ids)

    async def get_last_session_ids(self, user_id: int, provider: Optional[AgentProvider] = None) -> list[str]:
        storage = await self._get_storage()
        return await storage.state.get_last_session_ids(user_id, self._resolve_provider(provider))

    async def set_pending_session_pick(
        self,
        user_id: int,
        enabled: bool,
        provider: Optional[AgentProvider] = None,
    ) -> None:
        storage = await self._get_storage()
        await storage.state.set_pending_session_pick(user_id, self._resolve_provider(provider), enabled)

    async def is_pending_session_pick(self, user_id: int, provider: Optional[AgentProvider] = None) -> bool:
        storage = await self._get_storage()
        return await storage.state.is_pending_session_pick(user_id, self._resolve_provider(provider))

    async def set_pending_image(
        self,
        user_id: int,
        image: PendingImage,
        provider: Optional[AgentProvider] = None,
    ) -> None:
        storage = await self._get_storage()
        await storage.state.set_pending_image(user_id, self._resolve_provider(provider), image)

    async def get_pending_image(
        self,
        user_id: int,
        provider: Optional[AgentProvider] = None,
    ) -> Optional[PendingImage]:
        storage = await self._get_storage()
        return await storage.state.get_pending_image(user_id, self._resolve_provider(provider))

    async def clear_pending_image(
        self,
        user_id: int,
        provider: Optional[AgentProvider] = None,
    ) -> Optional[PendingImage]:
        storage = await self._get_storage()
        return await storage.state.clear_pending_image(user_id, self._resolve_provider(provider))

    async def set_active_run(
        self,
        user_id: int,
        active_run: Optional[ActiveRunState],
        provider: Optional[AgentProvider] = None,
    ) -> None:
        storage = await self._get_storage()
        await storage.state.set_active_run(user_id, self._resolve_provider(provider), active_run)

    async def get_active_run(
        self,
        user_id: int,
        provider: Optional[AgentProvider] = None,
    ) -> Optional[ActiveRunState]:
        storage = await self._get_storage()
        return await storage.state.get_active_run(user_id, self._resolve_provider(provider))

    async def clear_active_run(
        self,
        user_id: int,
        provider: Optional[AgentProvider] = None,
    ) -> Optional[ActiveRunState]:
        storage = await self._get_storage()
        return await storage.state.clear_active_run(user_id, self._resolve_provider(provider))

    async def set_pending_interaction(
        self,
        user_id: int,
        interaction: Optional[PendingInteraction],
        provider: Optional[AgentProvider] = None,
    ) -> None:
        storage = await self._get_storage()
        await storage.state.set_pending_interaction(user_id, self._resolve_provider(provider), interaction)

    async def get_pending_interaction(
        self,
        user_id: int,
        provider: Optional[AgentProvider] = None,
    ) -> Optional[PendingInteraction]:
        storage = await self._get_storage()
        return await storage.state.get_pending_interaction(user_id, self._resolve_provider(provider))

    async def clear_pending_interaction(
        self,
        user_id: int,
        provider: Optional[AgentProvider] = None,
    ) -> Optional[PendingInteraction]:
        storage = await self._get_storage()
        return await storage.state.clear_pending_interaction(user_id, self._resolve_provider(provider))

    async def record_run_result(
        self,
        *,
        user_id: int,
        provider: AgentProvider,
        run_id: str,
        status: str,
        cwd: Path,
        session_id_before: Optional[str],
        session_id_after: Optional[str],
        prompt: str,
        answer: str,
        stderr_text: str,
        return_code: int,
        attachment_ref_ids: tuple[int, ...] = (),
    ) -> None:
        storage = await self._get_storage()
        await storage.state.record_run_result(
            user_id=user_id,
            provider=provider,
            run_id=run_id,
            status=status,
            cwd=str(cwd),
            session_id_before=session_id_before,
            session_id_after=session_id_after,
            prompt=prompt,
            answer=answer,
            stderr_text=stderr_text,
            return_code=return_code,
            attachment_ref_ids=attachment_ref_ids,
        )

    async def record_interaction_result(self, interaction_id: str, status: str) -> None:
        storage = await self._get_storage()
        await storage.state.record_interaction_result(interaction_id, status)

    def _resolve_provider(self, provider: Optional[AgentProvider]) -> AgentProvider:
        if provider in _PROVIDERS:
            return provider
        return self.default_provider

    async def get_storage(self) -> StorageManager:
        return await self._get_storage()

    async def _get_storage(self) -> StorageManager:
        if self.storage is not None:
            return self.storage
        async with self._storage_lock:
            if self.storage is not None:
                return self.storage
            if self._storage_config is None:
                raise RuntimeError("state storage is not configured")
            self.storage = await StorageManager.open(self._storage_config)
            return self.storage
