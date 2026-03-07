from __future__ import annotations

import asyncio
import hashlib
import json
import os
import queue
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Optional, Sequence
from uuid import UUID

from ..domain.models import ActiveRunState, AgentProvider, PendingImage, PendingInteraction, SessionMeta

SCHEMA_VERSION = 1
PARSER_VERSION = 1
CONFIG_SNAPSHOT_RETENTION = 32
_PROVIDERS: tuple[AgentProvider, AgentProvider] = ("codex", "claude")
_STATS_COUNT_QUERIES: dict[str, str] = {
    "sessions": "SELECT COUNT(*) FROM sessions",
    "session_raw_lines": "SELECT COUNT(*) FROM session_raw_lines",
    "session_events": "SELECT COUNT(*) FROM session_events",
    "session_messages": "SELECT COUNT(*) FROM session_messages",
    "attachment_blobs": "SELECT COUNT(*) FROM attachment_blobs",
    "attachment_refs": "SELECT COUNT(*) FROM attachment_refs",
    "runs": "SELECT COUNT(*) FROM runs",
    "interactions": "SELECT COUNT(*) FROM interactions",
}


def _now_ts() -> int:
    return int(time.time())


def _normalize_path(path: Path) -> str:
    return str(path.expanduser().resolve())


def _json_dumps(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _safe_file_name(file_name: Optional[str], fallback: str = "attachment.bin") -> str:
    candidate = Path(file_name or "").name.strip()
    return candidate or fallback


def _compact_title(text: str, limit: int = 46) -> str:
    one_line = " ".join(text.split())
    if len(one_line) <= limit:
        return one_line
    return one_line[: limit - 1] + "…"


def compact_message(text: str, limit: int = 320) -> str:
    one_line = " ".join(text.split())
    if len(one_line) <= limit:
        return one_line
    return one_line[: limit - 1] + "…"


def _is_uuid(value: str) -> bool:
    try:
        UUID(value)
        return True
    except Exception:
        return False


@dataclass(frozen=True)
class _ParsedLine:
    parse_status: str
    parse_error: Optional[str]
    payload_json: Optional[str]
    event_type: Optional[str]
    provider_session_id: Optional[str]
    timestamp: Optional[str]
    cwd: Optional[str]
    messages: tuple[tuple[str, str], ...]


class _SQLiteWorker:
    def __init__(self, db_path: Path, init_callback: Callable[[sqlite3.Connection], None]):
        self.db_path = db_path
        self._init_callback = init_callback
        self._tasks: queue.Queue[tuple[Callable[..., Any], tuple[Any, ...], dict[str, Any], asyncio.Future[Any], asyncio.AbstractEventLoop] | None] = queue.Queue()
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._run, name="tiya-sqlite", daemon=True)
        self._startup_error: Optional[BaseException] = None
        self._thread.start()
        self._ready.wait()
        if self._startup_error is not None:
            raise RuntimeError("failed to initialize sqlite storage") from self._startup_error

    async def call(self, func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        loop = asyncio.get_running_loop()
        future: asyncio.Future[Any] = loop.create_future()
        self._tasks.put((func, args, kwargs, future, loop))
        return await future

    async def close(self) -> None:
        loop = asyncio.get_running_loop()
        future: asyncio.Future[None] = loop.create_future()

        def _close(conn: sqlite3.Connection) -> None:
            conn.close()

        self._tasks.put((_close, (), {}, future, loop))
        await future
        self._tasks.put(None)
        await asyncio.to_thread(self._thread.join)

    def _run(self) -> None:
        conn: Optional[sqlite3.Connection] = None
        try:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(self.db_path), timeout=5.0, isolation_level=None)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute("PRAGMA synchronous=NORMAL")
            self._init_callback(conn)
            self._ensure_permissions()
        except BaseException as exc:  # noqa: BLE001
            self._startup_error = exc
            self._ready.set()
            if conn is not None:
                conn.close()
            return

        self._ready.set()
        assert conn is not None
        while True:
            item = self._tasks.get()
            if item is None:
                return
            func, args, kwargs, future, loop = item
            try:
                result = func(conn, *args, **kwargs)
                self._ensure_permissions()
            except BaseException as exc:  # noqa: BLE001
                if not future.done():
                    loop.call_soon_threadsafe(future.set_exception, exc)
            else:
                if not future.done():
                    loop.call_soon_threadsafe(future.set_result, result)

    def _ensure_permissions(self) -> None:
        for path in (self.db_path, self.db_path.with_name(self.db_path.name + "-wal"), self.db_path.with_name(self.db_path.name + "-shm")):
            if path.exists():
                try:
                    os.chmod(path, 0o600)
                except OSError:
                    pass


class StorageManager:
    def __init__(
        self,
        db_path: Path,
        *,
        instance_id: str,
        default_provider: AgentProvider = "codex",
        attachments_root: Path,
        legacy_state_path: Optional[Path] = None,
        session_roots: Optional[dict[AgentProvider, Path]] = None,
        config_snapshot: Optional[dict[str, object]] = None,
        maintenance_mode: bool = False,
    ):
        self.db_path = db_path.expanduser()
        self.instance_id = instance_id
        self.default_provider = default_provider if default_provider in _PROVIDERS else "codex"
        self.attachments_root = attachments_root.expanduser()
        self.legacy_state_path = legacy_state_path.expanduser() if legacy_state_path is not None else None
        self.session_roots = {
            provider: path.expanduser().resolve()
            for provider, path in (session_roots or {}).items()
            if provider in _PROVIDERS
        }
        self.config_snapshot = dict(config_snapshot or {})
        self.maintenance_mode = maintenance_mode
        self._session_root_ids: dict[AgentProvider, int] = {}
        self._worker = _SQLiteWorker(self.db_path, self._bootstrap_sync)

    async def close(self) -> None:
        await self._worker.close()

    async def snapshot_config(self, payload: dict[str, object]) -> None:
        self.config_snapshot = dict(payload)
        await self._worker.call(self._snapshot_config_sync, self.config_snapshot)

    async def backup(self, destination: Path) -> None:
        await self._worker.call(self._backup_sync, destination.expanduser())

    async def vacuum(self) -> None:
        await self._worker.call(self._vacuum_sync)

    async def stats(self) -> dict[str, object]:
        return await self._worker.call(self._stats_sync)

    async def store_attachment_file(
        self,
        path: Path,
        *,
        file_name: str,
        mime_type: Optional[str],
        file_size: Optional[int],
        source_kind: str = "telegram",
    ) -> int:
        return await self._worker.call(
            self._store_attachment_file_sync,
            path.expanduser(),
            file_name,
            mime_type,
            file_size,
            source_kind,
        )

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
    ) -> None:
        await self._worker.call(
            self._record_run_result_sync,
            user_id,
            provider,
            run_id,
            status,
            str(cwd),
            session_id_before,
            session_id_after,
            prompt,
            answer,
            stderr_text,
            return_code,
        )

    async def record_interaction_result(self, interaction_id: str, status: str) -> None:
        await self._worker.call(self._record_interaction_result_sync, interaction_id, status)

    async def refresh_session_root(self, provider: AgentProvider, root: Path) -> None:
        await self._worker.call(self._refresh_session_root_sync, provider, root.expanduser().resolve())

    async def refresh_session(self, provider: AgentProvider, root: Path, session_id: str) -> None:
        await self._worker.call(self._refresh_session_sync, provider, root.expanduser().resolve(), session_id)

    async def list_recent_sessions(self, provider: AgentProvider, root: Path, limit: int) -> list[SessionMeta]:
        return await self._worker.call(self._list_recent_sessions_sync, provider, root.expanduser().resolve(), limit)

    async def find_session(self, provider: AgentProvider, root: Path, session_id: str) -> Optional[SessionMeta]:
        return await self._worker.call(self._find_session_sync, provider, root.expanduser().resolve(), session_id)

    async def get_session_history(
        self,
        provider: AgentProvider,
        root: Path,
        session_id: str,
        limit: int,
    ) -> tuple[Optional[SessionMeta], list[tuple[str, str]]]:
        return await self._worker.call(self._get_session_history_sync, provider, root.expanduser().resolve(), session_id, limit)

    async def get_active_provider(self, user_id: int) -> AgentProvider:
        return await self._worker.call(self._get_active_provider_sync, user_id)

    async def set_active_provider(self, user_id: int, provider: AgentProvider) -> None:
        await self._worker.call(self._set_active_provider_sync, user_id, provider)

    async def set_active_session(
        self,
        user_id: int,
        session_id: str,
        cwd: str,
        provider: AgentProvider,
    ) -> None:
        await self._worker.call(self._set_active_session_sync, user_id, provider, session_id, cwd)

    async def clear_active_session(self, user_id: int, cwd: str, provider: AgentProvider) -> None:
        await self._worker.call(self._clear_active_session_sync, user_id, provider, cwd)

    async def get_active(self, user_id: int, provider: AgentProvider) -> tuple[Optional[str], Optional[str]]:
        return await self._worker.call(self._get_active_sync, user_id, provider)

    async def set_last_session_ids(self, user_id: int, session_ids: list[str], provider: AgentProvider) -> None:
        await self._worker.call(self._set_last_session_ids_sync, user_id, provider, session_ids)

    async def get_last_session_ids(self, user_id: int, provider: AgentProvider) -> list[str]:
        return await self._worker.call(self._get_last_session_ids_sync, user_id, provider)

    async def set_pending_session_pick(self, user_id: int, enabled: bool, provider: AgentProvider) -> None:
        await self._worker.call(self._set_pending_session_pick_sync, user_id, provider, enabled)

    async def is_pending_session_pick(self, user_id: int, provider: AgentProvider) -> bool:
        return await self._worker.call(self._is_pending_session_pick_sync, user_id, provider)

    async def set_pending_image(self, user_id: int, image: PendingImage, provider: AgentProvider) -> None:
        await self._worker.call(self._set_pending_image_sync, user_id, provider, image)

    async def get_pending_image(self, user_id: int, provider: AgentProvider) -> Optional[PendingImage]:
        return await self._worker.call(self._get_pending_image_sync, user_id, provider)

    async def clear_pending_image(self, user_id: int, provider: AgentProvider) -> Optional[PendingImage]:
        return await self._worker.call(self._clear_pending_image_sync, user_id, provider)

    async def set_active_run(self, user_id: int, provider: AgentProvider, active_run: Optional[ActiveRunState]) -> None:
        await self._worker.call(self._set_active_run_sync, user_id, provider, active_run)

    async def get_active_run(self, user_id: int, provider: AgentProvider) -> Optional[ActiveRunState]:
        return await self._worker.call(self._get_active_run_sync, user_id, provider)

    async def clear_active_run(self, user_id: int, provider: AgentProvider) -> Optional[ActiveRunState]:
        return await self._worker.call(self._clear_active_run_sync, user_id, provider)

    async def set_pending_interaction(
        self,
        user_id: int,
        provider: AgentProvider,
        interaction: Optional[PendingInteraction],
    ) -> None:
        await self._worker.call(self._set_pending_interaction_sync, user_id, provider, interaction)

    async def get_pending_interaction(self, user_id: int, provider: AgentProvider) -> Optional[PendingInteraction]:
        return await self._worker.call(self._get_pending_interaction_sync, user_id, provider)

    async def clear_pending_interaction(self, user_id: int, provider: AgentProvider) -> Optional[PendingInteraction]:
        return await self._worker.call(self._clear_pending_interaction_sync, user_id, provider)

    def _bootstrap_sync(self, conn: sqlite3.Connection) -> None:
        self._ensure_schema_sync(conn)
        if self.maintenance_mode:
            return
        self._ensure_instance_sync(conn)
        self._sync_session_roots_sync(conn)
        if self.config_snapshot:
            self._snapshot_config_sync(conn, self.config_snapshot)
        if self.legacy_state_path is not None:
            self._maybe_import_legacy_state_sync(conn, self.legacy_state_path)
        self._recover_instance_state_sync(conn)

    def _ensure_schema_sync(self, conn: sqlite3.Connection) -> None:
        version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        if version > SCHEMA_VERSION:
            raise RuntimeError(f"unsupported sqlite schema version: {version}")
        while version < SCHEMA_VERSION:
            if version == 0:
                self._migrate_schema_v0_to_v1_sync(conn)
                version = 1
                continue
            raise RuntimeError(f"missing sqlite migration path from version {version} to {SCHEMA_VERSION}")

    def _migrate_schema_v0_to_v1_sync(self, conn: sqlite3.Connection) -> None:
        def _op() -> None:
            for statement in self._schema_v1_sql().split(";\n"):
                sql = statement.strip()
                if not sql:
                    continue
                conn.execute(sql)
            conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")

        self._write_tx_sync(conn, _op)

    @staticmethod
    def _schema_v1_sql() -> str:
        return """
            CREATE TABLE IF NOT EXISTS instances (
                instance_id TEXT PRIMARY KEY,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS config_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                instance_id TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                snapshot_json TEXT NOT NULL,
                FOREIGN KEY (instance_id) REFERENCES instances(instance_id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS session_roots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                provider TEXT NOT NULL,
                root_path TEXT NOT NULL,
                normalized_root TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                last_seen_at INTEGER NOT NULL,
                UNIQUE(provider, normalized_root)
            );
            CREATE TABLE IF NOT EXISTS session_sources (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_root_id INTEGER NOT NULL,
                provider TEXT NOT NULL,
                source_path TEXT NOT NULL,
                normalized_source_path TEXT NOT NULL,
                provider_session_id TEXT,
                mtime_ns INTEGER NOT NULL DEFAULT 0,
                size_bytes INTEGER NOT NULL DEFAULT 0,
                source_status TEXT NOT NULL DEFAULT 'live',
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                missing_since INTEGER,
                UNIQUE(session_root_id, normalized_source_path),
                FOREIGN KEY (session_root_id) REFERENCES session_roots(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_root_id INTEGER NOT NULL,
                source_id INTEGER NOT NULL UNIQUE,
                provider TEXT NOT NULL,
                provider_session_id TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                cwd TEXT NOT NULL,
                title TEXT NOT NULL,
                first_seen_at INTEGER NOT NULL,
                last_seen_at INTEGER NOT NULL,
                UNIQUE(session_root_id, provider_session_id),
                FOREIGN KEY (session_root_id) REFERENCES session_roots(id) ON DELETE CASCADE,
                FOREIGN KEY (source_id) REFERENCES session_sources(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS session_raw_lines (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_id INTEGER NOT NULL,
                line_no INTEGER NOT NULL,
                byte_offset INTEGER NOT NULL,
                raw_text TEXT NOT NULL,
                parse_status TEXT NOT NULL,
                parse_error TEXT,
                content_hash TEXT NOT NULL,
                UNIQUE(source_id, line_no),
                FOREIGN KEY (source_id) REFERENCES session_sources(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS session_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_id INTEGER NOT NULL,
                line_no INTEGER NOT NULL,
                provider TEXT NOT NULL,
                provider_session_id TEXT,
                event_type TEXT,
                event_timestamp TEXT,
                cwd TEXT,
                payload_json TEXT NOT NULL,
                UNIQUE(source_id, line_no),
                FOREIGN KEY (source_id) REFERENCES session_sources(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS session_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_id INTEGER NOT NULL,
                session_id INTEGER NOT NULL,
                line_no INTEGER NOT NULL,
                message_index INTEGER NOT NULL,
                role TEXT NOT NULL,
                content_text TEXT NOT NULL,
                message_timestamp TEXT,
                UNIQUE(source_id, line_no, message_index),
                FOREIGN KEY (source_id) REFERENCES session_sources(id) ON DELETE CASCADE,
                FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS session_import_cursors (
                source_id INTEGER PRIMARY KEY,
                parser_version INTEGER NOT NULL,
                mtime_ns INTEGER NOT NULL,
                size_bytes INTEGER NOT NULL,
                byte_offset INTEGER NOT NULL,
                line_count INTEGER NOT NULL,
                last_import_started_at INTEGER NOT NULL,
                last_import_finished_at INTEGER NOT NULL,
                last_status TEXT NOT NULL,
                last_error TEXT,
                FOREIGN KEY (source_id) REFERENCES session_sources(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS materialization_checkpoints (
                name TEXT PRIMARY KEY,
                checkpoint_value TEXT NOT NULL,
                updated_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS user_state (
                telegram_user_id INTEGER PRIMARY KEY,
                active_provider TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS attachment_blobs (
                sha256 TEXT PRIMARY KEY,
                data BLOB NOT NULL,
                size_bytes INTEGER NOT NULL,
                created_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS attachment_refs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                blob_sha256 TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                original_file_name TEXT NOT NULL,
                mime_type TEXT,
                file_size INTEGER,
                source_kind TEXT NOT NULL,
                FOREIGN KEY (blob_sha256) REFERENCES attachment_blobs(sha256) ON DELETE RESTRICT
            );
            CREATE TABLE IF NOT EXISTS provider_state (
                instance_id TEXT NOT NULL,
                telegram_user_id INTEGER NOT NULL,
                provider TEXT NOT NULL,
                session_root_id INTEGER,
                active_session_id TEXT,
                active_cwd TEXT,
                pending_session_pick INTEGER NOT NULL DEFAULT 0,
                pending_attachment_ref_id INTEGER,
                pending_attachment_legacy_path TEXT,
                pending_image_file_name TEXT,
                pending_image_mime_type TEXT,
                pending_image_file_size INTEGER,
                pending_image_message_id INTEGER,
                pending_image_created_at INTEGER,
                active_run_id TEXT,
                pending_interaction_id TEXT,
                PRIMARY KEY (instance_id, telegram_user_id, provider),
                FOREIGN KEY (instance_id) REFERENCES instances(instance_id) ON DELETE CASCADE,
                FOREIGN KEY (session_root_id) REFERENCES session_roots(id) ON DELETE SET NULL,
                FOREIGN KEY (pending_attachment_ref_id) REFERENCES attachment_refs(id) ON DELETE SET NULL
            );
            CREATE TABLE IF NOT EXISTS session_pick_cache (
                instance_id TEXT NOT NULL,
                telegram_user_id INTEGER NOT NULL,
                provider TEXT NOT NULL,
                position INTEGER NOT NULL,
                session_id TEXT NOT NULL,
                PRIMARY KEY (instance_id, telegram_user_id, provider, position),
                FOREIGN KEY (instance_id, telegram_user_id, provider)
                    REFERENCES provider_state(instance_id, telegram_user_id, provider) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS runs (
                run_id TEXT PRIMARY KEY,
                instance_id TEXT NOT NULL,
                telegram_user_id INTEGER NOT NULL,
                provider TEXT NOT NULL,
                chat_id INTEGER NOT NULL,
                chat_type TEXT NOT NULL,
                started_at INTEGER NOT NULL,
                finished_at INTEGER,
                status TEXT NOT NULL,
                cwd TEXT,
                session_id_before TEXT,
                session_id_after TEXT,
                prompt_len INTEGER,
                prompt_hash TEXT,
                answer_len INTEGER,
                answer_hash TEXT,
                stderr_len INTEGER,
                stderr_hash TEXT,
                return_code INTEGER,
                FOREIGN KEY (instance_id) REFERENCES instances(instance_id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS interactions (
                interaction_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                instance_id TEXT NOT NULL,
                telegram_user_id INTEGER NOT NULL,
                provider TEXT NOT NULL,
                kind TEXT NOT NULL,
                title TEXT NOT NULL,
                body TEXT NOT NULL,
                options_json TEXT NOT NULL,
                reply_mode TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                expires_at INTEGER NOT NULL,
                chat_id INTEGER NOT NULL,
                message_id INTEGER,
                status TEXT NOT NULL,
                FOREIGN KEY (run_id) REFERENCES runs(run_id) ON DELETE CASCADE,
                FOREIGN KEY (instance_id) REFERENCES instances(instance_id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS legacy_imports (
                source_path TEXT PRIMARY KEY,
                source_mtime_ns INTEGER NOT NULL,
                source_size INTEGER NOT NULL,
                imported_at INTEGER NOT NULL,
                status TEXT NOT NULL
            );
        """

    def _ensure_instance_sync(self, conn: sqlite3.Connection) -> None:
        now = _now_ts()
        conn.execute(
            """
            INSERT INTO instances (instance_id, created_at, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(instance_id) DO UPDATE SET updated_at=excluded.updated_at
            """,
            (self.instance_id, now, now),
        )

    def _snapshot_config_sync(self, conn: sqlite3.Connection, payload: dict[str, object]) -> None:
        conn.execute(
            "INSERT INTO config_snapshots (instance_id, created_at, snapshot_json) VALUES (?, ?, ?)",
            (self.instance_id, _now_ts(), _json_dumps(payload)),
        )
        conn.execute(
            """
            DELETE FROM config_snapshots
            WHERE instance_id=?
              AND id NOT IN (
                  SELECT id
                  FROM config_snapshots
                  WHERE instance_id=?
                  ORDER BY id DESC
                  LIMIT ?
              )
            """,
            (self.instance_id, self.instance_id, CONFIG_SNAPSHOT_RETENTION),
        )

    def _sync_session_roots_sync(self, conn: sqlite3.Connection) -> None:
        self._session_root_ids = {}
        for provider, root in self.session_roots.items():
            self._session_root_ids[provider] = self._upsert_session_root_sync(conn, provider, root)

    def _upsert_session_root_sync(self, conn: sqlite3.Connection, provider: AgentProvider, root: Path) -> int:
        normalized_root = _normalize_path(root)
        now = _now_ts()
        conn.execute(
            """
            INSERT INTO session_roots (provider, root_path, normalized_root, created_at, last_seen_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(provider, normalized_root) DO UPDATE SET
                root_path=excluded.root_path,
                last_seen_at=excluded.last_seen_at
            """,
            (provider, str(root), normalized_root, now, now),
        )
        row = conn.execute(
            "SELECT id FROM session_roots WHERE provider=? AND normalized_root=?",
            (provider, normalized_root),
        ).fetchone()
        assert row is not None
        return int(row["id"])

    def _recover_instance_state_sync(self, conn: sqlite3.Connection) -> None:
        now = _now_ts()
        conn.execute(
            """
            UPDATE runs
            SET status='aborted_on_boot', finished_at=COALESCE(finished_at, ?)
            WHERE instance_id=? AND status='running'
            """,
            (now, self.instance_id),
        )
        conn.execute(
            """
            UPDATE interactions
            SET status='expired_on_boot'
            WHERE instance_id=? AND status='pending'
            """,
            (self.instance_id,),
        )

        rows = conn.execute(
            "SELECT telegram_user_id, provider, session_root_id FROM provider_state WHERE instance_id=?",
            (self.instance_id,),
        ).fetchall()
        for row in rows:
            provider = str(row["provider"])
            current_root_id = self._session_root_ids.get(provider) if provider in _PROVIDERS else None
            if current_root_id is not None and row["session_root_id"] is not None and int(row["session_root_id"]) != current_root_id:
                conn.execute(
                    """
                    UPDATE provider_state
                    SET active_session_id=NULL,
                        session_root_id=?,
                        active_run_id=NULL,
                        pending_interaction_id=NULL
                    WHERE instance_id=? AND telegram_user_id=? AND provider=?
                    """,
                    (current_root_id, self.instance_id, int(row["telegram_user_id"]), provider),
                )
                conn.execute(
                    """
                    DELETE FROM session_pick_cache
                    WHERE instance_id=? AND telegram_user_id=? AND provider=?
                    """,
                    (self.instance_id, int(row["telegram_user_id"]), provider),
                )
                continue
            conn.execute(
                """
                UPDATE provider_state
                SET active_run_id=NULL, pending_interaction_id=NULL, session_root_id=COALESCE(?, session_root_id)
                WHERE instance_id=? AND telegram_user_id=? AND provider=?
                """,
                (current_root_id, self.instance_id, int(row["telegram_user_id"]), provider),
            )

    def _maybe_import_legacy_state_sync(self, conn: sqlite3.Connection, legacy_path: Path) -> None:
        if not legacy_path.is_file():
            return
        stat = legacy_path.stat()
        normalized = _normalize_path(legacy_path)
        existing = conn.execute(
            "SELECT source_mtime_ns, source_size, status FROM legacy_imports WHERE source_path=?",
            (normalized,),
        ).fetchone()
        if existing is not None and int(existing["source_mtime_ns"]) == int(stat.st_mtime_ns) and int(existing["source_size"]) == int(stat.st_size):
            return

        try:
            payload = json.loads(legacy_path.read_text(encoding="utf-8"))
        except Exception:
            conn.execute(
                """
                INSERT INTO legacy_imports (source_path, source_mtime_ns, source_size, imported_at, status)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(source_path) DO UPDATE SET
                    source_mtime_ns=excluded.source_mtime_ns,
                    source_size=excluded.source_size,
                    imported_at=excluded.imported_at,
                    status=excluded.status
                """,
                (normalized, int(stat.st_mtime_ns), int(stat.st_size), _now_ts(), "invalid_json"),
            )
            return

        users = payload.get("users")
        if not isinstance(users, dict):
            users = {}
        for raw_user_id, raw_user in users.items():
            try:
                user_id = int(str(raw_user_id))
            except ValueError:
                continue
            if not isinstance(raw_user, dict):
                continue
            active_provider = raw_user.get("active_provider")
            if active_provider not in _PROVIDERS:
                active_provider = "codex" if raw_user.get("active_session_id") else self.default_provider
            conn.execute(
                """
                INSERT INTO user_state (telegram_user_id, active_provider)
                VALUES (?, ?)
                ON CONFLICT(telegram_user_id) DO UPDATE SET active_provider=excluded.active_provider
                """,
                (user_id, active_provider),
            )
            providers = raw_user.get("providers")
            if not isinstance(providers, dict):
                providers = {}
            legacy_codex = {
                "active_session_id": raw_user.get("active_session_id"),
                "active_cwd": raw_user.get("active_cwd"),
                "last_session_ids": raw_user.get("last_session_ids"),
                "pending_session_pick": raw_user.get("pending_session_pick"),
            }
            if any(value is not None for value in legacy_codex.values()) and "codex" not in providers:
                providers["codex"] = legacy_codex
            for provider in _PROVIDERS:
                bucket = providers.get(provider)
                if not isinstance(bucket, dict):
                    continue
                self._import_legacy_provider_bucket_sync(conn, user_id, provider, bucket)

        conn.execute(
            """
            INSERT INTO legacy_imports (source_path, source_mtime_ns, source_size, imported_at, status)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(source_path) DO UPDATE SET
                source_mtime_ns=excluded.source_mtime_ns,
                source_size=excluded.source_size,
                imported_at=excluded.imported_at,
                status=excluded.status
            """,
            (normalized, int(stat.st_mtime_ns), int(stat.st_size), _now_ts(), "imported"),
        )

    def _import_legacy_provider_bucket_sync(
        self,
        conn: sqlite3.Connection,
        user_id: int,
        provider: AgentProvider,
        bucket: dict[str, object],
    ) -> None:
        self._ensure_provider_state_sync(conn, user_id, provider)
        current_root_id = self._current_session_root_id(provider)
        active_session_id = bucket.get("active_session_id")
        active_cwd = bucket.get("active_cwd")
        pending_pick = bool(bucket.get("pending_session_pick"))
        conn.execute(
            """
            UPDATE provider_state
            SET session_root_id=?,
                active_session_id=?,
                active_cwd=?,
                pending_session_pick=?
            WHERE instance_id=? AND telegram_user_id=? AND provider=?
            """,
            (
                current_root_id,
                active_session_id if isinstance(active_session_id, str) else None,
                active_cwd if isinstance(active_cwd, str) else None,
                1 if pending_pick else 0,
                self.instance_id,
                user_id,
                provider,
            ),
        )
        last_session_ids = bucket.get("last_session_ids")
        if isinstance(last_session_ids, list):
            self._set_last_session_ids_sync(conn, user_id, provider, [str(value) for value in last_session_ids if str(value).strip()])

        pending_image = bucket.get("pending_image")
        if isinstance(pending_image, dict):
            image = self._pending_image_from_legacy_dict(conn, pending_image)
            if image is not None:
                self._set_pending_image_sync(conn, user_id, provider, image)

        active_run = None
        raw_active_run = bucket.get("active_run")
        if isinstance(raw_active_run, dict):
            active_run = ActiveRunState.from_dict(raw_active_run)
        if active_run is not None:
            conn.execute(
                """
                INSERT INTO runs (run_id, instance_id, telegram_user_id, provider, chat_id, chat_type, started_at, finished_at, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id) DO NOTHING
                """,
                (
                    active_run.run_id,
                    self.instance_id,
                    user_id,
                    provider,
                    active_run.chat_id,
                    active_run.chat_type,
                    active_run.started_at,
                    _now_ts(),
                    "aborted_on_boot",
                ),
            )
        raw_interaction = bucket.get("pending_interaction")
        if isinstance(raw_interaction, dict):
            pending_interaction = PendingInteraction.from_dict(raw_interaction)
            if pending_interaction is not None:
                conn.execute(
                    """
                    INSERT INTO interactions (
                        interaction_id, run_id, instance_id, telegram_user_id, provider,
                        kind, title, body, options_json, reply_mode, created_at, expires_at, chat_id, message_id, status
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(interaction_id) DO NOTHING
                    """,
                    (
                        pending_interaction.interaction_id,
                        pending_interaction.run_id,
                        self.instance_id,
                        user_id,
                        provider,
                        pending_interaction.kind,
                        pending_interaction.title,
                        pending_interaction.body,
                        _json_dumps([option.__dict__ for option in pending_interaction.options]),
                        pending_interaction.reply_mode,
                        pending_interaction.created_at,
                        pending_interaction.expires_at,
                        pending_interaction.chat_id,
                        pending_interaction.message_id,
                        "expired_on_boot",
                    ),
                )

    def _pending_image_from_legacy_dict(self, conn: sqlite3.Connection, payload: dict[str, object]) -> Optional[PendingImage]:
        path_value = payload.get("path")
        file_name = payload.get("file_name")
        message_id = payload.get("message_id")
        created_at = payload.get("created_at")
        if not (isinstance(path_value, str) and isinstance(file_name, str) and isinstance(message_id, int) and isinstance(created_at, int)):
            return None
        path = Path(path_value)
        attachment_ref_id: Optional[int] = None
        if path.is_file():
            attachment_ref_id = self._store_attachment_file_sync(
                conn,
                path,
                file_name,
                payload.get("mime_type") if isinstance(payload.get("mime_type"), str) else None,
                payload.get("file_size") if isinstance(payload.get("file_size"), int) else None,
                "legacy_pending_image",
            )
        return PendingImage(
            path=path,
            file_name=file_name,
            mime_type=payload.get("mime_type") if isinstance(payload.get("mime_type"), str) else None,
            file_size=payload.get("file_size") if isinstance(payload.get("file_size"), int) else None,
            message_id=message_id,
            created_at=created_at,
            attachment_ref_id=attachment_ref_id,
        )

    def _write_tx_sync(self, conn: sqlite3.Connection, op: Callable[[], Any]) -> Any:
        conn.execute("BEGIN IMMEDIATE")
        try:
            result = op()
        except Exception:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")
        return result

    def _current_session_root_id(self, provider: AgentProvider) -> Optional[int]:
        return self._session_root_ids.get(provider)

    def _ensure_provider_state_sync(self, conn: sqlite3.Connection, user_id: int, provider: AgentProvider) -> None:
        conn.execute(
            """
            INSERT INTO user_state (telegram_user_id, active_provider)
            VALUES (?, ?)
            ON CONFLICT(telegram_user_id) DO NOTHING
            """,
            (user_id, self.default_provider),
        )
        conn.execute(
            """
            INSERT INTO provider_state (instance_id, telegram_user_id, provider, session_root_id)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(instance_id, telegram_user_id, provider) DO UPDATE SET
                session_root_id=COALESCE(excluded.session_root_id, provider_state.session_root_id)
            """,
            (self.instance_id, user_id, provider, self._current_session_root_id(provider)),
        )

    def _set_active_provider_sync(self, conn: sqlite3.Connection, user_id: int, provider: AgentProvider) -> None:
        self._write_tx_sync(
            conn,
            lambda: conn.execute(
                """
                INSERT INTO user_state (telegram_user_id, active_provider)
                VALUES (?, ?)
                ON CONFLICT(telegram_user_id) DO UPDATE SET active_provider=excluded.active_provider
                """,
                (user_id, provider),
            ),
        )

    def _get_active_provider_sync(self, conn: sqlite3.Connection, user_id: int) -> AgentProvider:
        row = conn.execute("SELECT active_provider FROM user_state WHERE telegram_user_id=?", (user_id,)).fetchone()
        if row is None:
            return self.default_provider
        provider = str(row["active_provider"])
        if provider in _PROVIDERS:
            return provider  # type: ignore[return-value]
        return self.default_provider

    def _set_active_session_sync(self, conn: sqlite3.Connection, user_id: int, provider: AgentProvider, session_id: str, cwd: str) -> None:
        def _op() -> None:
            self._ensure_provider_state_sync(conn, user_id, provider)
            conn.execute(
                """
                UPDATE provider_state
                SET session_root_id=?,
                    active_session_id=?,
                    active_cwd=?
                WHERE instance_id=? AND telegram_user_id=? AND provider=?
                """,
                (self._current_session_root_id(provider), session_id, cwd, self.instance_id, user_id, provider),
            )

        self._write_tx_sync(conn, _op)

    def _clear_active_session_sync(self, conn: sqlite3.Connection, user_id: int, provider: AgentProvider, cwd: str) -> None:
        def _op() -> None:
            self._ensure_provider_state_sync(conn, user_id, provider)
            conn.execute(
                """
                UPDATE provider_state
                SET session_root_id=?,
                    active_session_id=NULL,
                    active_cwd=?
                WHERE instance_id=? AND telegram_user_id=? AND provider=?
                """,
                (self._current_session_root_id(provider), cwd, self.instance_id, user_id, provider),
            )

        self._write_tx_sync(conn, _op)

    def _get_active_sync(self, conn: sqlite3.Connection, user_id: int, provider: AgentProvider) -> tuple[Optional[str], Optional[str]]:
        self._ensure_provider_state_sync(conn, user_id, provider)
        row = conn.execute(
            """
            SELECT active_session_id, active_cwd
            FROM provider_state
            WHERE instance_id=? AND telegram_user_id=? AND provider=?
            """,
            (self.instance_id, user_id, provider),
        ).fetchone()
        if row is None:
            return None, None
        session_id = row["active_session_id"]
        cwd = row["active_cwd"]
        return (session_id if isinstance(session_id, str) else None, cwd if isinstance(cwd, str) else None)

    def _set_last_session_ids_sync(self, conn: sqlite3.Connection, user_id: int, provider: AgentProvider, session_ids: Sequence[str]) -> None:
        def _op() -> None:
            self._ensure_provider_state_sync(conn, user_id, provider)
            conn.execute(
                "DELETE FROM session_pick_cache WHERE instance_id=? AND telegram_user_id=? AND provider=?",
                (self.instance_id, user_id, provider),
            )
            normalized_ids = [str(value) for value in session_ids if str(value).strip()]
            for index, session_id in enumerate(normalized_ids, start=1):
                conn.execute(
                    """
                    INSERT INTO session_pick_cache (instance_id, telegram_user_id, provider, position, session_id)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (self.instance_id, user_id, provider, index, session_id),
                )

        self._write_tx_sync(conn, _op)

    def _get_last_session_ids_sync(self, conn: sqlite3.Connection, user_id: int, provider: AgentProvider) -> list[str]:
        self._ensure_provider_state_sync(conn, user_id, provider)
        rows = conn.execute(
            """
            SELECT session_id FROM session_pick_cache
            WHERE instance_id=? AND telegram_user_id=? AND provider=?
            ORDER BY position ASC
            """,
            (self.instance_id, user_id, provider),
        ).fetchall()
        return [str(row["session_id"]) for row in rows]

    def _set_pending_session_pick_sync(self, conn: sqlite3.Connection, user_id: int, provider: AgentProvider, enabled: bool) -> None:
        def _op() -> None:
            self._ensure_provider_state_sync(conn, user_id, provider)
            conn.execute(
                """
                UPDATE provider_state
                SET pending_session_pick=?
                WHERE instance_id=? AND telegram_user_id=? AND provider=?
                """,
                (1 if enabled else 0, self.instance_id, user_id, provider),
            )

        self._write_tx_sync(conn, _op)

    def _is_pending_session_pick_sync(self, conn: sqlite3.Connection, user_id: int, provider: AgentProvider) -> bool:
        self._ensure_provider_state_sync(conn, user_id, provider)
        row = conn.execute(
            """
            SELECT pending_session_pick FROM provider_state
            WHERE instance_id=? AND telegram_user_id=? AND provider=?
            """,
            (self.instance_id, user_id, provider),
        ).fetchone()
        return bool(row["pending_session_pick"]) if row is not None else False

    def _store_attachment_file_sync(
        self,
        conn: sqlite3.Connection,
        path: Path,
        file_name: str,
        mime_type: Optional[str],
        file_size: Optional[int],
        source_kind: str,
    ) -> int:
        data = path.read_bytes()
        sha256 = _sha256_hex(data)
        now = _now_ts()
        conn.execute(
            """
            INSERT INTO attachment_blobs (sha256, data, size_bytes, created_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(sha256) DO NOTHING
            """,
            (sha256, data, len(data), now),
        )
        cursor = conn.execute(
            """
            INSERT INTO attachment_refs (blob_sha256, created_at, original_file_name, mime_type, file_size, source_kind)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (sha256, now, _safe_file_name(file_name), mime_type, file_size, source_kind),
        )
        return int(cursor.lastrowid)

    def _materialize_attachment_ref_sync(
        self,
        conn: sqlite3.Connection,
        attachment_ref_id: int,
        *,
        user_id: int,
        provider: AgentProvider,
        file_name: Optional[str],
    ) -> Path:
        row = conn.execute(
            """
            SELECT ref.original_file_name, blob.data
            FROM attachment_refs ref
            JOIN attachment_blobs blob ON blob.sha256=ref.blob_sha256
            WHERE ref.id=?
            """,
            (attachment_ref_id,),
        ).fetchone()
        if row is None:
            raise FileNotFoundError(f"attachment ref not found: {attachment_ref_id}")
        materialized_dir = self.attachments_root / f"user-{user_id}" / f"provider-{provider}" / f"pending-{attachment_ref_id}"
        materialized_dir.mkdir(parents=True, exist_ok=True)
        safe_name = _safe_file_name(file_name or str(row["original_file_name"]))
        path = materialized_dir / safe_name
        if path.exists():
            return path
        path.write_bytes(bytes(row["data"]))
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        return path

    def _set_pending_image_sync(self, conn: sqlite3.Connection, user_id: int, provider: AgentProvider, image: PendingImage) -> None:
        def _op() -> None:
            self._ensure_provider_state_sync(conn, user_id, provider)
            attachment_ref_id = image.attachment_ref_id
            legacy_path: Optional[str] = None
            if attachment_ref_id is None and image.path.is_file():
                attachment_ref_id = self._store_attachment_file_sync(
                    conn,
                    image.path,
                    image.file_name,
                    image.mime_type,
                    image.file_size,
                    "pending_image",
                )
            if attachment_ref_id is None:
                legacy_path = str(image.path)
            conn.execute(
                """
                UPDATE provider_state
                SET pending_attachment_ref_id=?,
                    pending_attachment_legacy_path=?,
                    pending_image_file_name=?,
                    pending_image_mime_type=?,
                    pending_image_file_size=?,
                    pending_image_message_id=?,
                    pending_image_created_at=?
                WHERE instance_id=? AND telegram_user_id=? AND provider=?
                """,
                (
                    attachment_ref_id,
                    legacy_path,
                    image.file_name,
                    image.mime_type,
                    image.file_size,
                    image.message_id,
                    image.created_at,
                    self.instance_id,
                    user_id,
                    provider,
                ),
            )

        self._write_tx_sync(conn, _op)

    def _pending_image_snapshot_from_row(self, row: sqlite3.Row) -> Optional[dict[str, object]]:
        file_name = row["pending_image_file_name"]
        message_id = row["pending_image_message_id"]
        created_at = row["pending_image_created_at"]
        if not (isinstance(file_name, str) and isinstance(message_id, int) and isinstance(created_at, int)):
            return None
        attachment_ref_id = row["pending_attachment_ref_id"]
        legacy_path = row["pending_attachment_legacy_path"]
        if not isinstance(attachment_ref_id, int) and not (isinstance(legacy_path, str) and legacy_path):
            return None
        return {
            "attachment_ref_id": int(attachment_ref_id) if isinstance(attachment_ref_id, int) else None,
            "legacy_path": str(legacy_path) if isinstance(legacy_path, str) else None,
            "file_name": file_name,
            "mime_type": row["pending_image_mime_type"] if isinstance(row["pending_image_mime_type"], str) else None,
            "file_size": row["pending_image_file_size"] if isinstance(row["pending_image_file_size"], int) else None,
            "message_id": message_id,
            "created_at": created_at,
        }

    def _pending_image_from_snapshot_sync(
        self,
        conn: sqlite3.Connection,
        user_id: int,
        provider: AgentProvider,
        snapshot: dict[str, object],
    ) -> PendingImage:
        attachment_ref_id = snapshot.get("attachment_ref_id")
        if isinstance(attachment_ref_id, int):
            path = self._materialize_attachment_ref_sync(
                conn,
                attachment_ref_id,
                user_id=user_id,
                provider=provider,
                file_name=str(snapshot["file_name"]),
            )
        else:
            path = Path(str(snapshot["legacy_path"]))
        return PendingImage(
            path=path,
            file_name=str(snapshot["file_name"]),
            mime_type=str(snapshot["mime_type"]) if isinstance(snapshot["mime_type"], str) else None,
            file_size=int(snapshot["file_size"]) if isinstance(snapshot["file_size"], int) else None,
            message_id=int(snapshot["message_id"]),
            created_at=int(snapshot["created_at"]),
            attachment_ref_id=int(attachment_ref_id) if isinstance(attachment_ref_id, int) else None,
        )

    def _read_pending_image_row_sync(self, conn: sqlite3.Connection, user_id: int, provider: AgentProvider) -> Optional[sqlite3.Row]:
        self._ensure_provider_state_sync(conn, user_id, provider)
        return conn.execute(
            """
            SELECT *
            FROM provider_state
            WHERE instance_id=? AND telegram_user_id=? AND provider=?
            """,
            (self.instance_id, user_id, provider),
        ).fetchone()

    def _get_pending_image_sync(self, conn: sqlite3.Connection, user_id: int, provider: AgentProvider) -> Optional[PendingImage]:
        row = self._read_pending_image_row_sync(conn, user_id, provider)
        if row is None:
            return None
        snapshot = self._pending_image_snapshot_from_row(row)
        if snapshot is None:
            return None
        return self._pending_image_from_snapshot_sync(conn, user_id, provider, snapshot)

    def _clear_pending_image_sync(self, conn: sqlite3.Connection, user_id: int, provider: AgentProvider) -> Optional[PendingImage]:
        snapshot: Optional[dict[str, object]] = None

        def _op() -> None:
            nonlocal snapshot
            self._ensure_provider_state_sync(conn, user_id, provider)
            row = self._read_pending_image_row_sync(conn, user_id, provider)
            if row is not None:
                snapshot = self._pending_image_snapshot_from_row(row)
            conn.execute(
                """
                UPDATE provider_state
                SET pending_attachment_ref_id=NULL,
                    pending_attachment_legacy_path=NULL,
                    pending_image_file_name=NULL,
                    pending_image_mime_type=NULL,
                    pending_image_file_size=NULL,
                    pending_image_message_id=NULL,
                    pending_image_created_at=NULL
                WHERE instance_id=? AND telegram_user_id=? AND provider=?
                """,
                (self.instance_id, user_id, provider),
            )

        self._write_tx_sync(conn, _op)
        if snapshot is None:
            return None
        return self._pending_image_from_snapshot_sync(conn, user_id, provider, snapshot)

    def _set_active_run_sync(self, conn: sqlite3.Connection, user_id: int, provider: AgentProvider, active_run: Optional[ActiveRunState]) -> None:
        def _op() -> None:
            self._ensure_provider_state_sync(conn, user_id, provider)
            if active_run is None:
                conn.execute(
                    """
                    UPDATE provider_state
                    SET active_run_id=NULL
                    WHERE instance_id=? AND telegram_user_id=? AND provider=?
                    """,
                    (self.instance_id, user_id, provider),
                )
                return
            conn.execute(
                """
                INSERT INTO runs (run_id, instance_id, telegram_user_id, provider, chat_id, chat_type, started_at, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'running')
                ON CONFLICT(run_id) DO UPDATE SET
                    chat_id=excluded.chat_id,
                    chat_type=excluded.chat_type,
                    started_at=excluded.started_at,
                    status='running'
                """,
                (
                    active_run.run_id,
                    self.instance_id,
                    user_id,
                    provider,
                    active_run.chat_id,
                    active_run.chat_type,
                    active_run.started_at,
                ),
            )
            conn.execute(
                """
                UPDATE provider_state
                SET active_run_id=?
                WHERE instance_id=? AND telegram_user_id=? AND provider=?
                """,
                (active_run.run_id, self.instance_id, user_id, provider),
            )

        self._write_tx_sync(conn, _op)

    def _get_active_run_sync(self, conn: sqlite3.Connection, user_id: int, provider: AgentProvider) -> Optional[ActiveRunState]:
        self._ensure_provider_state_sync(conn, user_id, provider)
        row = conn.execute(
            """
            SELECT run.run_id, run.chat_id, run.chat_type, run.started_at
            FROM provider_state state
            JOIN runs run ON run.run_id=state.active_run_id
            WHERE state.instance_id=? AND state.telegram_user_id=? AND state.provider=?
            """,
            (self.instance_id, user_id, provider),
        ).fetchone()
        if row is None:
            return None
        payload = {
            "run_id": row["run_id"],
            "chat_id": row["chat_id"],
            "chat_type": row["chat_type"],
            "started_at": row["started_at"],
        }
        return ActiveRunState.from_dict(payload)

    def _clear_active_run_sync(self, conn: sqlite3.Connection, user_id: int, provider: AgentProvider) -> Optional[ActiveRunState]:
        payload: Optional[dict[str, object]] = None

        def _op() -> None:
            nonlocal payload
            self._ensure_provider_state_sync(conn, user_id, provider)
            row = conn.execute(
                """
                SELECT run.run_id, run.chat_id, run.chat_type, run.started_at
                FROM provider_state state
                JOIN runs run ON run.run_id=state.active_run_id
                WHERE state.instance_id=? AND state.telegram_user_id=? AND state.provider=?
                """,
                (self.instance_id, user_id, provider),
            ).fetchone()
            if row is not None:
                payload = {
                    "run_id": row["run_id"],
                    "chat_id": row["chat_id"],
                    "chat_type": row["chat_type"],
                    "started_at": row["started_at"],
                }
            conn.execute(
                """
                UPDATE provider_state
                SET active_run_id=NULL
                WHERE instance_id=? AND telegram_user_id=? AND provider=?
                """,
                (self.instance_id, user_id, provider),
            )

        self._write_tx_sync(conn, _op)
        return ActiveRunState.from_dict(payload) if payload is not None else None

    def _set_pending_interaction_sync(
        self,
        conn: sqlite3.Connection,
        user_id: int,
        provider: AgentProvider,
        interaction: Optional[PendingInteraction],
    ) -> None:
        def _op() -> None:
            self._ensure_provider_state_sync(conn, user_id, provider)
            if interaction is None:
                conn.execute(
                    """
                    UPDATE provider_state
                    SET pending_interaction_id=NULL
                    WHERE instance_id=? AND telegram_user_id=? AND provider=?
                    """,
                    (self.instance_id, user_id, provider),
                )
                return
            conn.execute(
                """
                INSERT INTO interactions (
                    interaction_id, run_id, instance_id, telegram_user_id, provider,
                    kind, title, body, options_json, reply_mode, created_at, expires_at, chat_id, message_id, status
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending')
                ON CONFLICT(interaction_id) DO UPDATE SET
                    title=excluded.title,
                    body=excluded.body,
                    options_json=excluded.options_json,
                    reply_mode=excluded.reply_mode,
                    message_id=excluded.message_id,
                    status='pending'
                """,
                (
                    interaction.interaction_id,
                    interaction.run_id,
                    self.instance_id,
                    user_id,
                    provider,
                    interaction.kind,
                    interaction.title,
                    interaction.body,
                    _json_dumps([{"id": option.id, "label": option.label, "description": option.description} for option in interaction.options]),
                    interaction.reply_mode,
                    interaction.created_at,
                    interaction.expires_at,
                    interaction.chat_id,
                    interaction.message_id,
                ),
            )
            conn.execute(
                """
                UPDATE provider_state
                SET pending_interaction_id=?
                WHERE instance_id=? AND telegram_user_id=? AND provider=?
                """,
                (interaction.interaction_id, self.instance_id, user_id, provider),
            )

        self._write_tx_sync(conn, _op)

    def _get_pending_interaction_sync(self, conn: sqlite3.Connection, user_id: int, provider: AgentProvider) -> Optional[PendingInteraction]:
        self._ensure_provider_state_sync(conn, user_id, provider)
        row = conn.execute(
            """
            SELECT interaction.*
            FROM provider_state state
            JOIN interactions interaction ON interaction.interaction_id=state.pending_interaction_id
            WHERE state.instance_id=? AND state.telegram_user_id=? AND state.provider=?
            """,
            (self.instance_id, user_id, provider),
        ).fetchone()
        if row is None:
            return None
        return PendingInteraction.from_dict(
            {
                "interaction_id": row["interaction_id"],
                "run_id": row["run_id"],
                "kind": row["kind"],
                "title": row["title"],
                "body": row["body"],
                "options": json.loads(str(row["options_json"])),
                "reply_mode": row["reply_mode"],
                "created_at": row["created_at"],
                "expires_at": row["expires_at"],
                "chat_id": row["chat_id"],
                "message_id": row["message_id"],
            }
        )

    def _clear_pending_interaction_sync(self, conn: sqlite3.Connection, user_id: int, provider: AgentProvider) -> Optional[PendingInteraction]:
        payload: Optional[dict[str, object]] = None

        def _op() -> None:
            nonlocal payload
            self._ensure_provider_state_sync(conn, user_id, provider)
            row = conn.execute(
                """
                SELECT interaction.*
                FROM provider_state state
                JOIN interactions interaction ON interaction.interaction_id=state.pending_interaction_id
                WHERE state.instance_id=? AND state.telegram_user_id=? AND state.provider=?
                """,
                (self.instance_id, user_id, provider),
            ).fetchone()
            if row is not None:
                payload = {
                    "interaction_id": row["interaction_id"],
                    "run_id": row["run_id"],
                    "kind": row["kind"],
                    "title": row["title"],
                    "body": row["body"],
                    "options": json.loads(str(row["options_json"])),
                    "reply_mode": row["reply_mode"],
                    "created_at": row["created_at"],
                    "expires_at": row["expires_at"],
                    "chat_id": row["chat_id"],
                    "message_id": row["message_id"],
                }
            conn.execute(
                """
                UPDATE provider_state
                SET pending_interaction_id=NULL
                WHERE instance_id=? AND telegram_user_id=? AND provider=?
                """,
                (self.instance_id, user_id, provider),
            )

        self._write_tx_sync(conn, _op)
        return PendingInteraction.from_dict(payload) if payload is not None else None

    def _record_interaction_result_sync(self, conn: sqlite3.Connection, interaction_id: str, status: str) -> None:
        self._write_tx_sync(
            conn,
            lambda: conn.execute(
                "UPDATE interactions SET status=? WHERE interaction_id=?",
                (status, interaction_id),
            ),
        )

    def _record_run_result_sync(
        self,
        conn: sqlite3.Connection,
        user_id: int,
        provider: AgentProvider,
        run_id: str,
        status: str,
        cwd: str,
        session_id_before: Optional[str],
        session_id_after: Optional[str],
        prompt: str,
        answer: str,
        stderr_text: str,
        return_code: int,
    ) -> None:
        def _op() -> None:
            conn.execute(
                """
                UPDATE runs
                SET finished_at=?,
                    status=?,
                    cwd=?,
                    session_id_before=?,
                    session_id_after=?,
                    prompt_len=?,
                    prompt_hash=?,
                    answer_len=?,
                    answer_hash=?,
                    stderr_len=?,
                    stderr_hash=?,
                    return_code=?
                WHERE run_id=? AND instance_id=? AND telegram_user_id=? AND provider=?
                """,
                (
                    _now_ts(),
                    status,
                    cwd,
                    session_id_before,
                    session_id_after,
                    len(prompt),
                    _sha256_hex(prompt.encode("utf-8")),
                    len(answer),
                    _sha256_hex(answer.encode("utf-8")),
                    len(stderr_text),
                    _sha256_hex(stderr_text.encode("utf-8")),
                    return_code,
                    run_id,
                    self.instance_id,
                    user_id,
                    provider,
                ),
            )

        self._write_tx_sync(conn, _op)

    def _backup_sync(self, conn: sqlite3.Connection, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        backup_conn = sqlite3.connect(str(destination))
        try:
            conn.backup(backup_conn)
        finally:
            backup_conn.close()
        os.chmod(destination, 0o600)

    def _vacuum_sync(self, conn: sqlite3.Connection) -> None:
        conn.execute("VACUUM")

    def _stats_sync(self, conn: sqlite3.Connection) -> dict[str, object]:
        table_counts: dict[str, int] = {}
        for table_name, query in _STATS_COUNT_QUERIES.items():
            table_counts[table_name] = int(conn.execute(query).fetchone()[0])
        page_count = int(conn.execute("PRAGMA page_count").fetchone()[0])
        page_size = int(conn.execute("PRAGMA page_size").fetchone()[0])
        return {
            "db_path": str(self.db_path),
            "approx_size_bytes": page_count * page_size,
            "table_counts": table_counts,
        }

    def _ensure_session_root_sync(self, conn: sqlite3.Connection, provider: AgentProvider, root: Path) -> int:
        return self._upsert_session_root_sync(conn, provider, root)

    def _iter_session_files(self, provider: AgentProvider, root: Path) -> list[Path]:
        if not root.exists():
            return []
        if provider == "claude":
            files = [
                path
                for path in root.rglob("*.jsonl")
                if path.is_file() and "subagents" not in path.parts
            ]
        else:
            files = [path for path in root.rglob("*.jsonl") if path.is_file()]
        return sorted(files, key=lambda path: path.stat().st_mtime_ns, reverse=True)

    def _refresh_session_root_sync(self, conn: sqlite3.Connection, provider: AgentProvider, root: Path) -> None:
        root_id = self._ensure_session_root_sync(conn, provider, root)
        seen_paths: set[str] = set()
        for path in self._iter_session_files(provider, root):
            seen_paths.add(_normalize_path(path))
            self._import_source_sync(conn, provider, root_id, path)

        missing_rows = conn.execute(
            "SELECT id, normalized_source_path FROM session_sources WHERE session_root_id=?",
            (root_id,),
        ).fetchall()
        for row in missing_rows:
            normalized_path = str(row["normalized_source_path"])
            if normalized_path in seen_paths:
                continue
            self._write_tx_sync(
                conn,
                lambda row_id=int(row["id"]): conn.execute(
                    "UPDATE session_sources SET source_status='missing', missing_since=?, updated_at=? WHERE id=?",
                    (_now_ts(), _now_ts(), row_id),
                ),
            )

    def _refresh_session_sync(self, conn: sqlite3.Connection, provider: AgentProvider, root: Path, session_id: str) -> None:
        root_id = self._ensure_session_root_sync(conn, provider, root)
        row = conn.execute(
            """
            SELECT source.source_path
            FROM sessions session
            JOIN session_sources source ON source.id=session.source_id
            WHERE session.session_root_id=? AND session.provider=? AND session.provider_session_id=?
            """,
            (root_id, provider, session_id),
        ).fetchone()
        candidate_paths: list[Path] = []
        if row is not None and isinstance(row["source_path"], str):
            candidate_paths.append(Path(str(row["source_path"])))
        if provider == "claude" and _is_uuid(session_id):
            candidate_paths.extend(
                path for path in root.rglob(f"{session_id}.jsonl") if path.is_file() and "subagents" not in path.parts
            )
        if not candidate_paths:
            for path in self._iter_session_files(provider, root):
                if provider == "codex":
                    meta = self._scan_codex_meta(path)
                    if meta is not None and meta[0] == session_id:
                        candidate_paths.append(path)
                        break
        seen: set[str] = set()
        for path in candidate_paths:
            normalized = _normalize_path(path)
            if normalized in seen:
                continue
            seen.add(normalized)
            if path.exists():
                self._import_source_sync(conn, provider, root_id, path)
            else:
                self._write_tx_sync(
                    conn,
                    lambda normalized_path=normalized: conn.execute(
                        """
                        UPDATE session_sources
                        SET source_status='missing', missing_since=?, updated_at=?
                        WHERE session_root_id=? AND normalized_source_path=?
                        """,
                        (_now_ts(), _now_ts(), root_id, normalized_path),
                    ),
                )

    def _scan_codex_meta(self, path: Path) -> Optional[tuple[str, str, str]]:
        try:
            with path.open("r", encoding="utf-8") as fp:
                first_line = fp.readline()
            payload = json.loads(first_line)
        except Exception:
            return None
        if not isinstance(payload, dict) or payload.get("type") != "session_meta":
            return None
        body = payload.get("payload")
        if not isinstance(body, dict):
            return None
        session_id = body.get("id")
        if not isinstance(session_id, str) or not session_id:
            return None
        timestamp = body.get("timestamp") if isinstance(body.get("timestamp"), str) else "unknown"
        cwd = body.get("cwd") if isinstance(body.get("cwd"), str) else "unknown"
        return session_id, timestamp, cwd

    def _upsert_import_cursor_sync(
        self,
        conn: sqlite3.Connection,
        *,
        source_id: int,
        mtime_ns: int,
        size_bytes: int,
        byte_offset: int,
        line_count: int,
        started_at: int,
        finished_at: int,
        status: str,
        error: Optional[str],
    ) -> None:
        conn.execute(
            """
            INSERT INTO session_import_cursors (
                source_id, parser_version, mtime_ns, size_bytes, byte_offset, line_count,
                last_import_started_at, last_import_finished_at, last_status, last_error
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(source_id) DO UPDATE SET
                parser_version=excluded.parser_version,
                mtime_ns=excluded.mtime_ns,
                size_bytes=excluded.size_bytes,
                byte_offset=excluded.byte_offset,
                line_count=excluded.line_count,
                last_import_started_at=excluded.last_import_started_at,
                last_import_finished_at=excluded.last_import_finished_at,
                last_status=excluded.last_status,
                last_error=excluded.last_error
            """,
            (
                source_id,
                PARSER_VERSION,
                mtime_ns,
                size_bytes,
                byte_offset,
                line_count,
                started_at,
                finished_at,
                status,
                error,
            ),
        )

    def _import_source_sync(self, conn: sqlite3.Connection, provider: AgentProvider, root_id: int, path: Path) -> None:
        stat = path.stat()
        normalized_path = _normalize_path(path)
        now = _now_ts()
        source_row = conn.execute(
            """
            SELECT source.id, source.provider_session_id, cursor.parser_version, cursor.mtime_ns, cursor.size_bytes, cursor.byte_offset, cursor.line_count, cursor.last_status
            FROM session_sources source
            LEFT JOIN session_import_cursors cursor ON cursor.source_id=source.id
            WHERE source.session_root_id=? AND source.normalized_source_path=?
            """,
            (root_id, normalized_path),
        ).fetchone()
        full_reimport = False
        start_offset = 0
        start_line = 1
        source_id: int
        existing_session_row: Optional[sqlite3.Row] = None

        def _prepare_source() -> None:
            nonlocal source_id, full_reimport, start_offset, start_line, existing_session_row
            created_at = now
            if source_row is None:
                cursor = conn.execute(
                    """
                    INSERT INTO session_sources (
                        session_root_id, provider, source_path, normalized_source_path,
                        created_at, updated_at, mtime_ns, size_bytes, source_status
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'live')
                    """,
                    (root_id, provider, str(path), normalized_path, created_at, now, int(stat.st_mtime_ns), int(stat.st_size)),
                )
                source_id = int(cursor.lastrowid)
                full_reimport = True
                return
            source_id = int(source_row["id"])
            cursor_status = str(source_row["last_status"]) if isinstance(source_row["last_status"], str) else None
            if source_row["parser_version"] is None or cursor_status != "ok":
                full_reimport = True
            else:
                prev_mtime = int(source_row["mtime_ns"])
                prev_size = int(source_row["size_bytes"])
                if int(stat.st_size) < prev_size or int(stat.st_mtime_ns) < prev_mtime or int(source_row["parser_version"]) != PARSER_VERSION:
                    full_reimport = True
                elif int(stat.st_size) == prev_size and int(stat.st_mtime_ns) == prev_mtime:
                    full_reimport = False
                    start_offset = int(source_row["byte_offset"])
                    start_line = int(source_row["line_count"]) + 1
                    return
                else:
                    start_offset = int(source_row["byte_offset"])
                    start_line = int(source_row["line_count"]) + 1
            existing_session_row = conn.execute(
                "SELECT * FROM sessions WHERE source_id=?",
                (source_id,),
            ).fetchone()

        self._write_tx_sync(conn, _prepare_source)
        if source_row is not None and not full_reimport and int(stat.st_size) == int(source_row["size_bytes"]) and int(stat.st_mtime_ns) == int(source_row["mtime_ns"]):
            return

        def _op() -> None:
            nonlocal existing_session_row
            if full_reimport:
                conn.execute("DELETE FROM session_messages WHERE source_id=?", (source_id,))
                conn.execute("DELETE FROM session_events WHERE source_id=?", (source_id,))
                conn.execute("DELETE FROM session_raw_lines WHERE source_id=?", (source_id,))
                conn.execute("DELETE FROM sessions WHERE source_id=?", (source_id,))
                start_offset_local = 0
                start_line_local = 1
                session_id: Optional[str] = path.stem if provider == "claude" and _is_uuid(path.stem) else None
                timestamp = "unknown"
                cwd = "unknown"
                title: Optional[str] = None
                message_index = 0
            else:
                start_offset_local = start_offset
                start_line_local = start_line
                session_id = str(existing_session_row["provider_session_id"]) if existing_session_row is not None else (path.stem if provider == "claude" and _is_uuid(path.stem) else None)
                timestamp = str(existing_session_row["timestamp"]) if existing_session_row is not None else "unknown"
                cwd = str(existing_session_row["cwd"]) if existing_session_row is not None else "unknown"
                title = str(existing_session_row["title"]) if existing_session_row is not None else None
                row = conn.execute("SELECT COALESCE(MAX(message_index), -1) AS max_idx FROM session_messages WHERE source_id=?", (source_id,)).fetchone()
                message_index = int(row["max_idx"]) + 1 if row is not None else 0
            pending_messages: list[tuple[int, int, str, str, Optional[str]]] = []

            self._upsert_import_cursor_sync(
                conn,
                source_id=source_id,
                mtime_ns=int(stat.st_mtime_ns),
                size_bytes=int(stat.st_size),
                byte_offset=start_offset_local,
                line_count=start_line_local - 1,
                started_at=now,
                finished_at=now,
                status="running",
                error=None,
            )

            byte_offset = start_offset_local
            line_no = start_line_local
            with path.open("rb") as fp:
                if start_offset_local:
                    fp.seek(start_offset_local)
                while True:
                    raw = fp.readline()
                    if raw == b"":
                        break
                    next_offset = fp.tell()
                    if raw.endswith(b"\r\n"):
                        raw_line = raw[:-2]
                    elif raw.endswith(b"\n"):
                        raw_line = raw[:-1]
                    else:
                        raw_line = raw
                    raw_text = raw_line.decode("utf-8", errors="replace")
                    parsed = self._parse_session_line(provider, path, raw_text)
                    conn.execute(
                        """
                        INSERT INTO session_raw_lines (
                            source_id, line_no, byte_offset, raw_text, parse_status, parse_error, content_hash
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(source_id, line_no) DO UPDATE SET
                            byte_offset=excluded.byte_offset,
                            raw_text=excluded.raw_text,
                            parse_status=excluded.parse_status,
                            parse_error=excluded.parse_error,
                            content_hash=excluded.content_hash
                        """,
                        (source_id, line_no, byte_offset, raw_text, parsed.parse_status, parsed.parse_error, _sha256_hex(raw_line)),
                    )
                    if parsed.payload_json is not None:
                        conn.execute(
                            """
                            INSERT INTO session_events (
                                source_id, line_no, provider, provider_session_id, event_type, event_timestamp, cwd, payload_json
                            )
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                            ON CONFLICT(source_id, line_no) DO UPDATE SET
                                provider_session_id=excluded.provider_session_id,
                                event_type=excluded.event_type,
                                event_timestamp=excluded.event_timestamp,
                                cwd=excluded.cwd,
                                payload_json=excluded.payload_json
                            """,
                            (
                                source_id,
                                line_no,
                                provider,
                                parsed.provider_session_id,
                                parsed.event_type,
                                parsed.timestamp,
                                parsed.cwd,
                                parsed.payload_json,
                            ),
                        )
                    if session_id is None and parsed.provider_session_id:
                        session_id = parsed.provider_session_id
                    if timestamp == "unknown" and parsed.timestamp:
                        timestamp = parsed.timestamp
                    if cwd == "unknown" and parsed.cwd:
                        cwd = parsed.cwd
                    for role, text in parsed.messages:
                        if title is None and role == "user":
                            title = _compact_title(text)
                        pending_messages.append((line_no, message_index, role, text, parsed.timestamp))
                        message_index += 1
                    line_no += 1
                    byte_offset = next_offset

            title_value = title or (f"session {session_id[:8]}" if isinstance(session_id, str) and session_id else "unknown session")
            session_row_id: Optional[int] = None
            if session_id:
                conn.execute(
                    """
                    INSERT INTO sessions (
                        session_root_id, source_id, provider, provider_session_id, timestamp, cwd, title, first_seen_at, last_seen_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(source_id) DO UPDATE SET
                        provider_session_id=excluded.provider_session_id,
                        timestamp=excluded.timestamp,
                        cwd=excluded.cwd,
                        title=excluded.title,
                        last_seen_at=excluded.last_seen_at
                    """,
                    (root_id, source_id, provider, session_id, timestamp, cwd, title_value, now, now),
                )
                session_row_id = int(conn.execute("SELECT id FROM sessions WHERE source_id=?", (source_id,)).fetchone()["id"])
            if session_row_id is not None:
                for line_no_value, message_index_value, role, text, message_timestamp in pending_messages:
                    conn.execute(
                        """
                        INSERT INTO session_messages (
                            source_id, session_id, line_no, message_index, role, content_text, message_timestamp
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            source_id,
                            session_row_id,
                            line_no_value,
                            message_index_value,
                            role,
                            text,
                            message_timestamp,
                        ),
                    )
            conn.execute(
                """
                UPDATE session_sources
                SET provider_session_id=?,
                    source_path=?,
                    mtime_ns=?,
                    size_bytes=?,
                    source_status='live',
                    missing_since=NULL,
                    updated_at=?
                WHERE id=?
                """,
                (session_id, str(path), int(stat.st_mtime_ns), int(stat.st_size), _now_ts(), source_id),
            )
            self._upsert_import_cursor_sync(
                conn,
                source_id=source_id,
                mtime_ns=int(stat.st_mtime_ns),
                size_bytes=int(stat.st_size),
                byte_offset=byte_offset,
                line_count=line_no - 1,
                started_at=now,
                finished_at=_now_ts(),
                status="ok",
                error=None,
            )

        try:
            self._write_tx_sync(conn, _op)
        except Exception as exc:
            error_offset = 0 if full_reimport else start_offset
            error_line_count = 0 if full_reimport else max(start_line - 1, 0)
            self._write_tx_sync(
                conn,
                lambda: self._upsert_import_cursor_sync(
                    conn,
                    source_id=source_id,
                    mtime_ns=int(stat.st_mtime_ns),
                    size_bytes=int(stat.st_size),
                    byte_offset=error_offset,
                    line_count=error_line_count,
                    started_at=now,
                    finished_at=_now_ts(),
                    status="error",
                    error=repr(exc),
                ),
            )
            raise

    def _parse_session_line(self, provider: AgentProvider, path: Path, raw_text: str) -> _ParsedLine:
        if raw_text == "":
            return _ParsedLine("empty", None, None, None, None, None, None, ())
        try:
            payload = json.loads(raw_text)
        except json.JSONDecodeError as exc:
            return _ParsedLine("invalid_json", str(exc), None, None, None, None, None, ())
        if not isinstance(payload, dict):
            return _ParsedLine("parsed", None, _json_dumps(payload), None, None, None, None, ())

        if provider == "codex":
            event_type = payload.get("type") if isinstance(payload.get("type"), str) else None
            body = payload.get("payload")
            if not isinstance(body, dict):
                body = {}
            provider_session_id = body.get("id") if event_type == "session_meta" and isinstance(body.get("id"), str) else None
            timestamp = body.get("timestamp") if event_type == "session_meta" and isinstance(body.get("timestamp"), str) else None
            cwd = body.get("cwd") if event_type == "session_meta" and isinstance(body.get("cwd"), str) else None
            messages: tuple[tuple[str, str], ...] = ()
            if event_type == "event_msg":
                msg_type = body.get("type")
                message = (body.get("message") or "").strip()
                if msg_type == "user_message" and message:
                    messages = (("user", message),)
                elif msg_type == "agent_message" and message:
                    messages = (("assistant", message),)
            return _ParsedLine("parsed", None, _json_dumps(payload), event_type, provider_session_id, timestamp, cwd, messages)

        event_type = payload.get("type") if isinstance(payload.get("type"), str) else None
        provider_session_id = payload.get("sessionId") if isinstance(payload.get("sessionId"), str) else None
        if provider_session_id is None and _is_uuid(path.stem):
            provider_session_id = path.stem
        timestamp = payload.get("timestamp") if isinstance(payload.get("timestamp"), str) else None
        cwd = payload.get("cwd") if isinstance(payload.get("cwd"), str) else None
        messages: list[tuple[str, str]] = []
        if event_type == "user" and not bool(payload.get("isMeta")):
            text = self._extract_claude_text(payload.get("message"))
            if text:
                messages.append(("user", text))
        elif event_type == "assistant":
            text = self._extract_claude_text(payload.get("message"))
            if text:
                messages.append(("assistant", text))
        return _ParsedLine("parsed", None, _json_dumps(payload), event_type, provider_session_id, timestamp, cwd, tuple(messages))

    def _extract_claude_text(self, message: object) -> str:
        if not isinstance(message, dict):
            return ""
        content = message.get("content")
        if isinstance(content, str):
            return content.strip()
        if not isinstance(content, list):
            return ""
        parts: list[str] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") != "text":
                continue
            text = block.get("text")
            if isinstance(text, str) and text:
                parts.append(text)
        return "".join(parts).strip()

    def _session_root_id_for_query(self, conn: sqlite3.Connection, provider: AgentProvider, root: Path) -> Optional[int]:
        row = conn.execute(
            "SELECT id FROM session_roots WHERE provider=? AND normalized_root=?",
            (provider, _normalize_path(root)),
        ).fetchone()
        return int(row["id"]) if row is not None else None

    def _list_recent_sessions_sync(self, conn: sqlite3.Connection, provider: AgentProvider, root: Path, limit: int) -> list[SessionMeta]:
        root_id = self._session_root_id_for_query(conn, provider, root)
        if root_id is None:
            return []
        rows = conn.execute(
            """
            SELECT session.provider_session_id, session.timestamp, session.cwd, session.title, source.source_path
            FROM sessions session
            JOIN session_sources source ON source.id=session.source_id
            WHERE session.session_root_id=?
            ORDER BY source.mtime_ns DESC, session.id DESC
            LIMIT ?
            """,
            (root_id, max(1, limit)),
        ).fetchall()
        return [
            SessionMeta(
                session_id=str(row["provider_session_id"]),
                timestamp=str(row["timestamp"]),
                cwd=str(row["cwd"]),
                file_path=str(row["source_path"]),
                title=str(row["title"]),
            )
            for row in rows
        ]

    def _find_session_sync(self, conn: sqlite3.Connection, provider: AgentProvider, root: Path, session_id: str) -> Optional[SessionMeta]:
        root_id = self._session_root_id_for_query(conn, provider, root)
        if root_id is None:
            return None
        row = conn.execute(
            """
            SELECT session.provider_session_id, session.timestamp, session.cwd, session.title, source.source_path
            FROM sessions session
            JOIN session_sources source ON source.id=session.source_id
            WHERE session.session_root_id=? AND session.provider_session_id=?
            """,
            (root_id, session_id),
        ).fetchone()
        if row is None:
            return None
        return SessionMeta(
            session_id=str(row["provider_session_id"]),
            timestamp=str(row["timestamp"]),
            cwd=str(row["cwd"]),
            file_path=str(row["source_path"]),
            title=str(row["title"]),
        )

    def _get_session_history_sync(
        self,
        conn: sqlite3.Connection,
        provider: AgentProvider,
        root: Path,
        session_id: str,
        limit: int,
    ) -> tuple[Optional[SessionMeta], list[tuple[str, str]]]:
        meta = self._find_session_sync(conn, provider, root, session_id)
        if meta is None:
            return None, []
        root_id = self._session_root_id_for_query(conn, provider, root)
        assert root_id is not None
        session_row = conn.execute(
            "SELECT id FROM sessions WHERE session_root_id=? AND provider_session_id=?",
            (root_id, session_id),
        ).fetchone()
        if session_row is None:
            return meta, []
        rows = conn.execute(
            """
            SELECT role, content_text
            FROM session_messages
            WHERE session_id=?
            ORDER BY line_no ASC, message_index ASC
            """,
            (int(session_row["id"]),),
        ).fetchall()
        messages = [(str(row["role"]), str(row["content_text"])) for row in rows]
        if limit > 0:
            messages = messages[-limit:]
        return meta, messages
