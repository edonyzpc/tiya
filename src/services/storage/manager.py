from __future__ import annotations

import os
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import aiosqlite

from ...domain.models import AgentProvider
from .attachments import AttachmentStorage
from .maintenance import MaintenanceStorage
from .runtime import StorageRuntime, await_db, delete_sqlite_files, sqlite_managed_paths
from .schema import ensure_schema
from .sessions import SessionStorage
from .state import StateStorage

_PROVIDERS: tuple[AgentProvider, AgentProvider] = ("codex", "claude")


@dataclass(frozen=True)
class StorageConfig:
    db_path: Path
    instance_id: str
    default_provider: AgentProvider = "codex"
    attachments_root: Path = Path("attachments")
    session_roots: dict[AgentProvider, Path] = field(default_factory=dict)
    config_snapshot: Optional[dict[str, object]] = None
    maintenance_mode: bool = False

    def normalized(self) -> "StorageConfig":
        return StorageConfig(
            db_path=self.db_path.expanduser(),
            instance_id=self.instance_id,
            default_provider=self.default_provider if self.default_provider in _PROVIDERS else "codex",
            attachments_root=self.attachments_root.expanduser(),
            session_roots={
                provider: path.expanduser().resolve()
                for provider, path in self.session_roots.items()
                if provider in _PROVIDERS
            },
            config_snapshot=dict(self.config_snapshot or {}),
            maintenance_mode=self.maintenance_mode,
        )


class StorageManager:
    def __init__(
        self,
        config: StorageConfig,
        runtime: StorageRuntime,
        attachments: AttachmentStorage,
        sessions: SessionStorage,
        state: StateStorage,
        maintenance: MaintenanceStorage,
    ):
        self.config = config
        self.runtime = runtime
        self.attachments = attachments
        self.sessions = sessions
        self.state = state
        self.maintenance = maintenance
        self.db_path = config.db_path
        self.instance_id = config.instance_id
        self.default_provider = config.default_provider
        self.attachments_root = config.attachments_root
        self.session_roots = config.session_roots
        self.maintenance_mode = config.maintenance_mode

    @classmethod
    async def open(cls, config: StorageConfig) -> "StorageManager":
        normalized = config.normalized()
        runtime = await StorageRuntime.open(normalized.db_path)
        attachments = AttachmentStorage(runtime, normalized.attachments_root)
        sessions = SessionStorage(runtime, attachments)
        state = StateStorage(
            runtime,
            instance_id=normalized.instance_id,
            default_provider=normalized.default_provider,
            attachments=attachments,
            current_session_root_id=sessions.current_root_id,
        )
        maintenance = MaintenanceStorage(runtime, normalized.db_path, attachments)
        manager = cls(normalized, runtime, attachments, sessions, state, maintenance)
        try:
            await ensure_schema(runtime)
            if not normalized.maintenance_mode:
                await state.ensure_instance()
                await sessions.sync_session_roots(normalized.session_roots)
                if normalized.config_snapshot:
                    await maintenance.snapshot_config(normalized.instance_id, normalized.config_snapshot)
                await state.recover_instance_state()
        except Exception:
            await runtime.close()
            raise
        return manager

    @classmethod
    async def rebuild_database(
        cls,
        *,
        db_path: Path,
        instance_id: str,
        default_provider: AgentProvider = "codex",
        attachments_root: Path = Path("attachments"),
        session_roots: Optional[dict[AgentProvider, Path]] = None,
        config_snapshot: Optional[dict[str, object]] = None,
    ) -> tuple[Path, Optional[Path]]:
        return await rebuild_storage_database(
            StorageConfig(
                db_path=db_path,
                instance_id=instance_id,
                default_provider=default_provider,
                attachments_root=attachments_root,
                session_roots=session_roots or {},
                config_snapshot=config_snapshot,
            )
        )

    async def close(self) -> None:
        await self.runtime.close()


async def rebuild_storage_database(config: StorageConfig) -> tuple[Path, Optional[Path]]:
    normalized = config.normalized()
    resolved_db_path = normalized.db_path
    timestamp = time.strftime("%Y%m%d%H%M%S", time.gmtime())
    temp_path = resolved_db_path.with_name(f"{resolved_db_path.name}.rebuild-{timestamp}.tmp")
    backup_path = resolved_db_path.with_name(f"{resolved_db_path.name}.bak-{timestamp}")
    previous_db_exists = resolved_db_path.exists()
    delete_sqlite_files(temp_path)
    temp_config = StorageConfig(
        db_path=temp_path,
        instance_id=normalized.instance_id,
        default_provider=normalized.default_provider,
        attachments_root=normalized.attachments_root,
        session_roots=normalized.session_roots,
        config_snapshot=normalized.config_snapshot,
        maintenance_mode=False,
    )
    manager = await StorageManager.open(temp_config)
    try:
        for provider, root in manager.session_roots.items():
            await manager.sessions.refresh_session_root(provider, root)
        await manager.maintenance.checkpoint_truncate()
    except Exception:
        await manager.close()
        delete_sqlite_files(temp_path)
        raise
    await manager.close()

    if previous_db_exists:
        await _checkpoint_existing_database(resolved_db_path)
        os.replace(resolved_db_path, backup_path)
    try:
        os.replace(temp_path, resolved_db_path)
        delete_sqlite_files(temp_path)
    except Exception:
        if previous_db_exists and backup_path.exists():
            os.replace(backup_path, resolved_db_path)
        delete_sqlite_files(temp_path)
        raise
    return resolved_db_path, (backup_path if previous_db_exists else None)


async def _checkpoint_existing_database(db_path: Path) -> None:
    conn: Optional[aiosqlite.Connection] = None
    try:
        conn = await await_db(
            "rebuild_open_existing",
            aiosqlite.connect(str(db_path), timeout=5.0, isolation_level=None),
        )
        conn.row_factory = sqlite3.Row
        cursor = await await_db("rebuild_existing_checkpoint", conn.execute("PRAGMA wal_checkpoint(TRUNCATE)"))
        await await_db("rebuild_existing_checkpoint_close", cursor.close())
    finally:
        if conn is not None:
            await await_db("rebuild_close_existing", conn.close())
    for path in sqlite_managed_paths(db_path)[1:]:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
