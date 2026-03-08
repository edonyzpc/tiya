from __future__ import annotations
import hashlib
import json
import time
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Sequence
from uuid import UUID

from ...domain.models import AgentProvider, SessionMeta
from .attachments import (
    AttachmentSeed,
    AttachmentStorage,
    attachment_seed_from_data_url,
    attachment_seed_from_local_path,
    extension_for_mime,
)
from .runtime import StorageRuntime, StorageSession
from .schema import PARSER_VERSION


def _now_ts() -> int:
    return int(time.time())


def _normalize_path(path: Path) -> str:
    return str(path.expanduser().resolve())


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _compress_raw_line(data: bytes) -> tuple[str, bytes]:
    if not data:
        return "identity", b""
    compressed = zlib.compress(data, level=6)
    if len(compressed) + 8 >= len(data):
        return "identity", data
    return "zlib", compressed


def _decompress_raw_line(codec: str, payload: bytes) -> bytes:
    if codec == "identity":
        return payload
    if codec == "zlib":
        return zlib.decompress(payload)
    raise ValueError(f"unsupported raw codec: {codec}")


def _compact_title(text: str, limit: int = 46) -> str:
    one_line = " ".join(text.split())
    if len(one_line) <= limit:
        return one_line
    return one_line[: max(0, limit - 3)] + "..."


def compact_message(text: str, limit: int = 320) -> str:
    one_line = " ".join(text.split())
    if len(one_line) <= limit:
        return one_line
    return one_line[: max(0, limit - 3)] + "..."


def _is_uuid(value: str) -> bool:
    try:
        UUID(value)
        return True
    except Exception:
        return False


@dataclass(frozen=True)
class ParsedLine:
    parse_status: str
    parse_error: Optional[str]
    event_type: Optional[str]
    provider_session_id: Optional[str]
    timestamp: Optional[str]
    cwd: Optional[str]
    messages: tuple[tuple[str, str], ...]
    payload: Optional[dict[str, Any]]


@dataclass(frozen=True)
class PendingInlineMedia:
    message_text: str
    attachments: tuple[AttachmentSeed, ...]


@dataclass(frozen=True)
class ExistingSessionState:
    provider_session_id: Optional[str]
    timestamp: str
    cwd: str
    title: Optional[str]
    first_seen_at: Optional[int]


@dataclass(frozen=True)
class PreparedSourceImport:
    source_id: int
    unchanged: bool
    existing_session: Optional[ExistingSessionState]


@dataclass(frozen=True)
class RawLineRecord:
    line_no: int
    byte_offset: int
    raw_blob: bytes
    raw_codec: str
    raw_sha256: str
    parse_status: str
    parse_error: Optional[str]
    event_type: Optional[str]
    provider_session_id: Optional[str]
    event_timestamp: Optional[str]
    cwd: Optional[str]


@dataclass(frozen=True)
class MessageRecord:
    line_no: int
    message_index: int
    role: str
    content_text: str
    message_timestamp: Optional[str]
    attachment_seeds: tuple[AttachmentSeed, ...]


@dataclass(frozen=True)
class ProjectionSnapshot:
    session_id: Optional[str]
    timestamp: str
    cwd: str
    title: str
    first_seen_at: int
    messages: tuple[MessageRecord, ...]


@dataclass(frozen=True)
class SourceImportSnapshot:
    stat_mtime_ns: int
    stat_size: int
    raw_lines: tuple[RawLineRecord, ...]
    projection: ProjectionSnapshot
    byte_offset: int
    line_count: int


@dataclass(frozen=True)
class ArchivedRawLineRecord:
    line_no: int
    raw_blob: bytes
    raw_codec: str
    raw_sha256: str


@dataclass(frozen=True)
class RebuildProjectionInput:
    root_id: int
    source_id: int
    source_path: Path
    known_session_id: Optional[str]
    first_seen_at: Optional[int]
    raw_lines: tuple[ArchivedRawLineRecord, ...]


class SessionStorage:
    def __init__(self, runtime: StorageRuntime, attachments: AttachmentStorage):
        self.runtime = runtime
        self.attachments = attachments
        self._session_root_ids: dict[AgentProvider, int] = {}

    def current_root_id(self, provider: AgentProvider) -> Optional[int]:
        return self._session_root_ids.get(provider)

    async def sync_session_roots(self, session_roots: dict[AgentProvider, Path]) -> None:
        async def _sync(db: StorageSession) -> dict[AgentProvider, int]:
            root_ids: dict[AgentProvider, int] = {}
            for provider, root in session_roots.items():
                root_ids[provider] = await self._upsert_session_root(db, provider, root)
            return root_ids

        self._session_root_ids = await self.runtime.write(_sync)

    async def refresh_session_root(self, provider: AgentProvider, root: Path) -> None:
        resolved_root = root.expanduser().resolve()
        files = self._iter_session_files(provider, resolved_root)
        seen_paths = {_normalize_path(path) for path in files}
        root_id = await self.runtime.write(lambda db: self._ensure_session_root(db, provider, resolved_root))
        for path in files:
            try:
                await self._import_source(provider, root_id, path)
            except FileNotFoundError:
                continue
        await self.runtime.write(lambda db: self._mark_missing_sources_archived(db, root_id, seen_paths))

    async def refresh_session(self, provider: AgentProvider, root: Path, session_id: str) -> None:
        resolved_root = root.expanduser().resolve()
        root_id = await self.runtime.write(lambda db: self._ensure_session_root(db, provider, resolved_root))

        async def _lookup_source_path(db: StorageSession) -> Optional[str]:
            row = await db.fetch_one(
                """
                SELECT source.source_path
                FROM sessions session
                JOIN session_sources source ON source.id=session.source_id
                WHERE session.session_root_id=? AND session.provider=? AND session.provider_session_id=?
                """,
                (root_id, provider, session_id),
                op_name="refresh_session_lookup",
            )
            if row is None or not isinstance(row["source_path"], str):
                return None
            return str(row["source_path"])

        source_path = await self.runtime.read(_lookup_source_path)
        candidate_paths: list[Path] = []
        if source_path:
            candidate_paths.append(Path(source_path))
        if provider == "claude" and _is_uuid(session_id) and resolved_root.exists():
            candidate_paths.extend(
                path
                for path in resolved_root.rglob(f"{session_id}.jsonl")
                if path.is_file() and "subagents" not in path.parts
            )
        if not candidate_paths and provider == "codex":
            for path in self._iter_session_files(provider, resolved_root):
                meta = self._scan_codex_meta(path)
                if meta is not None and meta[0] == session_id:
                    candidate_paths.append(path)
                    break
        seen: set[str] = set()
        for path in candidate_paths:
            normalized_path = _normalize_path(path)
            if normalized_path in seen:
                continue
            seen.add(normalized_path)
            if path.exists():
                try:
                    await self._import_source(provider, root_id, path)
                except FileNotFoundError:
                    continue
                continue
            await self.runtime.write(
                lambda db, normalized_path=normalized_path: self._mark_source_archived(db, root_id, normalized_path)
            )

    async def rebuild_session_projection(self, provider: AgentProvider, root: Path, session_id: str) -> None:
        resolved_root = root.expanduser().resolve()
        rebuild_input = await self.runtime.read(
            lambda db: self._load_rebuild_projection_input(db, provider, resolved_root, session_id)
        )
        if rebuild_input is None:
            return
        projection = await self._build_projection_from_archive(
            provider,
            rebuild_input.source_path,
            rebuild_input.raw_lines,
            known_session_id=rebuild_input.known_session_id,
            first_seen_at=rebuild_input.first_seen_at or _now_ts(),
        )
        await self.runtime.write(
            lambda db: self._apply_projection_snapshot(
                db,
                provider=provider,
                root_id=rebuild_input.root_id,
                source_id=rebuild_input.source_id,
                source_path=rebuild_input.source_path,
                projection=projection,
                op_prefix="rebuild",
            )
        )

    async def list_recent_sessions(self, provider: AgentProvider, root: Path, limit: int) -> list[SessionMeta]:
        resolved_root = root.expanduser().resolve()

        async def _list(db: StorageSession) -> list[SessionMeta]:
            root_id = await self._session_root_id_for_query(db, provider, resolved_root)
            if root_id is None:
                return []
            rows = await db.fetch_all(
                """
                SELECT session.provider_session_id, session.timestamp, session.cwd, session.title, source.source_path
                FROM sessions session
                JOIN session_sources source ON source.id=session.source_id
                WHERE session.session_root_id=?
                ORDER BY source.mtime_ns DESC, session.id DESC
                LIMIT ?
                """,
                (root_id, max(1, limit)),
                op_name="list_recent_sessions",
            )
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

        return await self.runtime.read(_list)

    async def find_session(self, provider: AgentProvider, root: Path, session_id: str) -> Optional[SessionMeta]:
        resolved_root = root.expanduser().resolve()
        return await self.runtime.read(lambda db: self._find_session(db, provider, resolved_root, session_id))

    async def get_session_history(
        self,
        provider: AgentProvider,
        root: Path,
        session_id: str,
        limit: int,
    ) -> tuple[Optional[SessionMeta], list[tuple[str, str]]]:
        resolved_root = root.expanduser().resolve()

        async def _history(db: StorageSession) -> tuple[Optional[SessionMeta], list[tuple[str, str]]]:
            meta = await self._find_session(db, provider, resolved_root, session_id)
            if meta is None:
                return None, []
            root_id = await self._session_root_id_for_query(db, provider, resolved_root)
            assert root_id is not None
            session_row = await db.fetch_one(
                "SELECT id FROM sessions WHERE session_root_id=? AND provider_session_id=?",
                (root_id, session_id),
                op_name="session_history_session_row",
            )
            if session_row is None:
                return meta, []
            rows = await db.fetch_all(
                """
                SELECT id, role, content_text
                FROM session_messages
                WHERE session_id=?
                ORDER BY line_no ASC, message_index ASC
                """,
                (int(session_row["id"]),),
                op_name="session_history_rows",
            )
            messages: list[tuple[str, str]] = []
            for row in rows:
                attachment_rows = await db.fetch_all(
                    """
                    SELECT ref.original_file_name
                    FROM session_message_attachments sma
                    JOIN attachment_refs ref ON ref.id=sma.attachment_ref_id
                    WHERE sma.message_id=?
                    ORDER BY sma.attachment_index ASC
                    """,
                    (int(row["id"]),),
                    op_name="session_history_attachments",
                )
                attachment_names = [str(attachment_row["original_file_name"]) for attachment_row in attachment_rows]
                text = self._message_text_with_attachment_names(str(row["content_text"]), attachment_names)
                messages.append((str(row["role"]), text))
            return meta, messages

        meta, messages = await self.runtime.read(_history)
        if limit > 0:
            messages = messages[-limit:]
        return meta, messages

    async def _ensure_session_root(self, db: StorageSession, provider: AgentProvider, root: Path) -> int:
        root_id = await self._upsert_session_root(db, provider, root)
        self._session_root_ids[provider] = root_id
        return root_id

    async def _upsert_session_root(self, db: StorageSession, provider: AgentProvider, root: Path) -> int:
        normalized_root = _normalize_path(root)
        now = _now_ts()
        await db.execute(
            """
            INSERT INTO session_roots (provider, root_path, normalized_root, created_at, last_seen_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(provider, normalized_root) DO UPDATE SET
                root_path=excluded.root_path,
                last_seen_at=excluded.last_seen_at
            """,
            (provider, str(root), normalized_root, now, now),
            op_name="upsert_session_root",
        )
        row = await db.fetch_one(
            "SELECT id FROM session_roots WHERE provider=? AND normalized_root=?",
            (provider, normalized_root),
            op_name="get_session_root_id",
        )
        assert row is not None
        return int(row["id"])

    def _iter_session_files(self, provider: AgentProvider, root: Path) -> list[Path]:
        if not root.exists():
            return []
        files: list[tuple[int, Path]] = []
        for path in root.rglob("*.jsonl"):
            try:
                if not path.is_file():
                    continue
                if provider == "claude" and "subagents" in path.parts:
                    continue
                files.append((int(path.stat().st_mtime_ns), path))
            except OSError:
                continue
        files.sort(key=lambda item: item[0], reverse=True)
        return [path for _mtime_ns, path in files]

    async def _session_root_id_for_query(self, db: StorageSession, provider: AgentProvider, root: Path) -> Optional[int]:
        row = await db.fetch_one(
            "SELECT id FROM session_roots WHERE provider=? AND normalized_root=?",
            (provider, _normalize_path(root)),
            op_name="session_root_id_for_query",
        )
        return int(row["id"]) if row is not None else None

    async def _find_session(
        self,
        db: StorageSession,
        provider: AgentProvider,
        root: Path,
        session_id: str,
    ) -> Optional[SessionMeta]:
        root_id = await self._session_root_id_for_query(db, provider, root)
        if root_id is None:
            return None
        row = await db.fetch_one(
            """
            SELECT session.provider_session_id, session.timestamp, session.cwd, session.title, source.source_path
            FROM sessions session
            JOIN session_sources source ON source.id=session.source_id
            WHERE session.session_root_id=? AND session.provider_session_id=?
            """,
            (root_id, session_id),
            op_name="find_session",
        )
        if row is None:
            return None
        return SessionMeta(
            session_id=str(row["provider_session_id"]),
            timestamp=str(row["timestamp"]),
            cwd=str(row["cwd"]),
            file_path=str(row["source_path"]),
            title=str(row["title"]),
        )

    async def _import_source(self, provider: AgentProvider, root_id: int, path: Path) -> None:
        stat = path.stat()
        started_at = _now_ts()
        if await self.runtime.read(
            lambda db: self._source_import_is_unchanged(
                db,
                root_id=root_id,
                path=path,
                stat_mtime_ns=int(stat.st_mtime_ns),
                stat_size=int(stat.st_size),
            )
        ):
            return
        prepared = await self.runtime.write(
            lambda db: self._prepare_source_import(
                db,
                provider=provider,
                root_id=root_id,
                path=path,
                stat_mtime_ns=int(stat.st_mtime_ns),
                stat_size=int(stat.st_size),
                started_at=started_at,
            )
        )
        if prepared.unchanged:
            return
        try:
            snapshot = await self._build_source_import_snapshot(
                provider,
                path,
                existing_session=prepared.existing_session,
                started_at=started_at,
                initial_stat_mtime_ns=int(stat.st_mtime_ns),
            )
        except FileNotFoundError:
            raise
        except Exception as exc:
            await self._mark_import_error(
                source_id=prepared.source_id,
                mtime_ns=int(stat.st_mtime_ns),
                size_bytes=int(stat.st_size),
                started_at=started_at,
                error=repr(exc),
            )
            raise
        try:
            await self.runtime.write(
                lambda db: self._apply_source_import_snapshot(
                    db,
                    provider=provider,
                    root_id=root_id,
                    source_id=prepared.source_id,
                    source_path=path,
                    snapshot=snapshot,
                    started_at=started_at,
                )
            )
        except Exception as exc:
            await self._mark_import_error(
                source_id=prepared.source_id,
                mtime_ns=snapshot.stat_mtime_ns,
                size_bytes=snapshot.stat_size,
                started_at=started_at,
                error=repr(exc),
            )
            raise

    async def _source_import_is_unchanged(
        self,
        db: StorageSession,
        *,
        root_id: int,
        path: Path,
        stat_mtime_ns: int,
        stat_size: int,
    ) -> bool:
        row = await db.fetch_one(
            """
            SELECT
                source.mtime_ns,
                source.size_bytes,
                source.source_status,
                cursor.parser_version,
                cursor.last_status
            FROM session_sources source
            LEFT JOIN session_import_cursors cursor ON cursor.source_id=source.id
            WHERE source.session_root_id=? AND source.normalized_source_path=?
            """,
            (root_id, _normalize_path(path)),
            op_name="import_source_probe",
        )
        if row is None:
            return False
        return self._is_source_unchanged(row, stat_mtime_ns=stat_mtime_ns, stat_size=stat_size)

    async def _prepare_source_import(
        self,
        db: StorageSession,
        *,
        provider: AgentProvider,
        root_id: int,
        path: Path,
        stat_mtime_ns: int,
        stat_size: int,
        started_at: int,
    ) -> PreparedSourceImport:
        normalized_path = _normalize_path(path)
        now = _now_ts()
        source_row = await db.fetch_one(
            """
            SELECT
                source.id,
                source.mtime_ns,
                source.size_bytes,
                source.source_status,
                cursor.parser_version,
                cursor.last_status,
                session.provider_session_id AS existing_provider_session_id,
                session.timestamp AS existing_timestamp,
                session.cwd AS existing_cwd,
                session.title AS existing_title,
                session.first_seen_at AS existing_first_seen_at
            FROM session_sources source
            LEFT JOIN session_import_cursors cursor ON cursor.source_id=source.id
            LEFT JOIN sessions session ON session.source_id=source.id
            WHERE source.session_root_id=? AND source.normalized_source_path=?
            """,
            (root_id, normalized_path),
            op_name="import_source_lookup",
        )
        existing_session: Optional[ExistingSessionState] = None
        if source_row is None:
            source_id = await db.execute_insert(
                """
                INSERT INTO session_sources (
                    session_root_id, provider, source_path, normalized_source_path,
                    created_at, updated_at, mtime_ns, size_bytes, source_status
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'live')
                """,
                (root_id, provider, str(path), normalized_path, started_at, now, stat_mtime_ns, stat_size),
                op_name="insert_session_source",
            )
            return PreparedSourceImport(source_id=source_id, unchanged=False, existing_session=None)

        source_id = int(source_row["id"])
        if source_row["existing_timestamp"] is not None:
            existing_session = ExistingSessionState(
                provider_session_id=(
                    str(source_row["existing_provider_session_id"])
                    if isinstance(source_row["existing_provider_session_id"], str)
                    else None
                ),
                timestamp=str(source_row["existing_timestamp"]),
                cwd=str(source_row["existing_cwd"]),
                title=str(source_row["existing_title"]) if source_row["existing_title"] is not None else None,
                first_seen_at=(
                    int(source_row["existing_first_seen_at"])
                    if source_row["existing_first_seen_at"] is not None
                    else None
                ),
            )
        unchanged = self._is_source_unchanged(source_row, stat_mtime_ns=stat_mtime_ns, stat_size=stat_size)
        if unchanged:
            return PreparedSourceImport(source_id=source_id, unchanged=True, existing_session=existing_session)
        await db.execute(
            """
            UPDATE session_sources
            SET source_path=?, mtime_ns=?, size_bytes=?, updated_at=?
            WHERE id=?
            """,
            (str(path), stat_mtime_ns, stat_size, now, source_id),
            op_name="touch_session_source",
        )
        return PreparedSourceImport(source_id=source_id, unchanged=unchanged, existing_session=existing_session)

    @staticmethod
    def _is_source_unchanged(row: Any, *, stat_mtime_ns: int, stat_size: int) -> bool:
        return (
            int(row["mtime_ns"]) == stat_mtime_ns
            and int(row["size_bytes"]) == stat_size
            and int(row["parser_version"] or 0) == PARSER_VERSION
            and str(row["last_status"] or "") == "ok"
            and str(row["source_status"] or "") == "live"
        )

    async def _build_source_import_snapshot(
        self,
        provider: AgentProvider,
        path: Path,
        *,
        existing_session: Optional[ExistingSessionState],
        started_at: int,
        initial_stat_mtime_ns: int,
    ) -> SourceImportSnapshot:
        data = path.read_bytes()
        try:
            stat_after = path.stat()
            stat_mtime_ns = int(stat_after.st_mtime_ns)
        except OSError:
            stat_mtime_ns = initial_stat_mtime_ns
        raw_lines: list[RawLineRecord] = []
        parsed_entries: list[tuple[int, ParsedLine]] = []
        byte_offset = 0
        line_no = 1
        for chunk in data.splitlines(keepends=True):
            if chunk.endswith(b"\r\n"):
                raw_line = chunk[:-2]
            elif chunk.endswith(b"\n"):
                raw_line = chunk[:-1]
            else:
                raw_line = chunk
            parsed = self._parse_session_line(provider, path, raw_line.decode("utf-8", errors="replace"))
            raw_codec, raw_blob = _compress_raw_line(raw_line)
            raw_lines.append(
                RawLineRecord(
                    line_no=line_no,
                    byte_offset=byte_offset,
                    raw_blob=raw_blob,
                    raw_codec=raw_codec,
                    raw_sha256=_sha256_hex(raw_line),
                    parse_status=parsed.parse_status,
                    parse_error=parsed.parse_error,
                    event_type=parsed.event_type,
                    provider_session_id=parsed.provider_session_id,
                    event_timestamp=parsed.timestamp,
                    cwd=parsed.cwd,
                )
            )
            parsed_entries.append((line_no, parsed))
            byte_offset += len(chunk)
            line_no += 1
        projection = await self._build_projection_snapshot(
            provider,
            path,
            parsed_entries,
            existing_session=existing_session,
            first_seen_at=(existing_session.first_seen_at if existing_session is not None else None) or started_at,
        )
        return SourceImportSnapshot(
            stat_mtime_ns=stat_mtime_ns,
            stat_size=len(data),
            raw_lines=tuple(raw_lines),
            projection=projection,
            byte_offset=byte_offset,
            line_count=len(raw_lines),
        )

    async def _apply_source_import_snapshot(
        self,
        db: StorageSession,
        *,
        provider: AgentProvider,
        root_id: int,
        source_id: int,
        source_path: Path,
        snapshot: SourceImportSnapshot,
        started_at: int,
    ) -> None:
        await self._upsert_import_cursor(
            db,
            source_id=source_id,
            mtime_ns=snapshot.stat_mtime_ns,
            size_bytes=snapshot.stat_size,
            byte_offset=0,
            line_count=0,
            started_at=started_at,
            finished_at=started_at,
            status="running",
            error=None,
        )
        await db.execute("DELETE FROM session_raw_lines WHERE source_id=?", (source_id,), op_name="delete_session_raw_lines")
        if snapshot.raw_lines:
            await db.executemany(
                """
                INSERT INTO session_raw_lines (
                    source_id, line_no, byte_offset, raw_blob, raw_codec, raw_sha256,
                    parse_status, parse_error, event_type, provider_session_id, event_timestamp, cwd
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_id, line_no) DO UPDATE SET
                    byte_offset=excluded.byte_offset,
                    raw_blob=excluded.raw_blob,
                    raw_codec=excluded.raw_codec,
                    raw_sha256=excluded.raw_sha256,
                    parse_status=excluded.parse_status,
                    parse_error=excluded.parse_error,
                    event_type=excluded.event_type,
                    provider_session_id=excluded.provider_session_id,
                    event_timestamp=excluded.event_timestamp,
                    cwd=excluded.cwd
                """,
                [
                    (
                        source_id,
                        raw_line.line_no,
                        raw_line.byte_offset,
                        raw_line.raw_blob,
                        raw_line.raw_codec,
                        raw_line.raw_sha256,
                        raw_line.parse_status,
                        raw_line.parse_error,
                        raw_line.event_type,
                        raw_line.provider_session_id,
                        raw_line.event_timestamp,
                        raw_line.cwd,
                    )
                    for raw_line in snapshot.raw_lines
                ],
                op_name="insert_session_raw_line_batch",
            )

        await self._apply_projection_snapshot(
            db,
            provider=provider,
            root_id=root_id,
            source_id=source_id,
            source_path=source_path,
            projection=snapshot.projection,
            op_prefix="import",
        )
        await db.execute(
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
            (
                snapshot.projection.session_id,
                str(source_path),
                snapshot.stat_mtime_ns,
                snapshot.stat_size,
                _now_ts(),
                source_id,
            ),
            op_name="finalize_session_source",
        )
        await self._upsert_import_cursor(
            db,
            source_id=source_id,
            mtime_ns=snapshot.stat_mtime_ns,
            size_bytes=snapshot.stat_size,
            byte_offset=snapshot.byte_offset,
            line_count=snapshot.line_count,
            started_at=started_at,
            finished_at=_now_ts(),
            status="ok",
            error=None,
        )

    async def _load_rebuild_projection_input(
        self,
        db: StorageSession,
        provider: AgentProvider,
        root: Path,
        session_id: str,
    ) -> Optional[RebuildProjectionInput]:
        root_id = await self._session_root_id_for_query(db, provider, root)
        if root_id is None:
            return None
        row = await db.fetch_one(
            """
            SELECT source.id, source.source_path, source.provider_session_id
            FROM session_sources source
            WHERE source.session_root_id=? AND source.provider=? AND source.provider_session_id=?
            """,
            (root_id, provider, session_id),
            op_name="rebuild_projection_source_lookup",
        )
        if row is None:
            row = await db.fetch_one(
                """
                SELECT source.id, source.source_path, source.provider_session_id
                FROM sessions session
                JOIN session_sources source ON source.id=session.source_id
                WHERE session.session_root_id=? AND session.provider=? AND session.provider_session_id=?
                """,
                (root_id, provider, session_id),
                op_name="rebuild_projection_session_lookup",
            )
        if row is None:
            return None
        existing_session_row = await db.fetch_one(
            "SELECT first_seen_at FROM sessions WHERE source_id=?",
            (int(row["id"]),),
            op_name="rebuild_existing_session_row",
        )
        raw_rows = await db.fetch_all(
            """
            SELECT line_no, raw_blob, raw_codec, raw_sha256
            FROM session_raw_lines
            WHERE source_id=?
            ORDER BY line_no ASC
            """,
            (int(row["id"]),),
            op_name="rebuild_raw_lines",
        )
        return RebuildProjectionInput(
            root_id=root_id,
            source_id=int(row["id"]),
            source_path=Path(str(row["source_path"])),
            known_session_id=(
                str(row["provider_session_id"]) if isinstance(row["provider_session_id"], str) else None
            ),
            first_seen_at=(
                int(existing_session_row["first_seen_at"])
                if existing_session_row is not None and existing_session_row["first_seen_at"] is not None
                else None
            ),
            raw_lines=tuple(
                ArchivedRawLineRecord(
                    line_no=int(raw_row["line_no"]),
                    raw_blob=bytes(raw_row["raw_blob"]),
                    raw_codec=str(raw_row["raw_codec"]),
                    raw_sha256=str(raw_row["raw_sha256"]),
                )
                for raw_row in raw_rows
            ),
        )

    async def _build_projection_from_archive(
        self,
        provider: AgentProvider,
        source_path: Path,
        raw_lines: Sequence[ArchivedRawLineRecord],
        *,
        known_session_id: Optional[str],
        first_seen_at: int,
    ) -> ProjectionSnapshot:
        parsed_entries: list[tuple[int, ParsedLine]] = []
        for row in raw_lines:
            raw_line = _decompress_raw_line(row.raw_codec, row.raw_blob)
            if _sha256_hex(raw_line) != row.raw_sha256:
                raise RuntimeError(
                    f"session archive checksum mismatch for source_id rebuild line_no={row.line_no}"
                )
            parsed_entries.append(
                (
                    row.line_no,
                    self._parse_session_line(provider, source_path, raw_line.decode("utf-8", errors="replace")),
                )
            )
        existing_session = ExistingSessionState(
            provider_session_id=known_session_id,
            timestamp="unknown",
            cwd="unknown",
            title=None,
            first_seen_at=first_seen_at,
        )
        return await self._build_projection_snapshot(
            provider,
            source_path,
            parsed_entries,
            existing_session=existing_session,
            first_seen_at=first_seen_at,
        )

    async def _build_projection_snapshot(
        self,
        provider: AgentProvider,
        source_path: Path,
        parsed_entries: Sequence[tuple[int, ParsedLine]],
        *,
        existing_session: Optional[ExistingSessionState],
        first_seen_at: int,
    ) -> ProjectionSnapshot:
        session_id: Optional[str] = (
            source_path.stem if provider == "claude" and _is_uuid(source_path.stem) else None
        )
        if existing_session is not None and existing_session.provider_session_id:
            session_id = existing_session.provider_session_id
        timestamp = existing_session.timestamp if existing_session is not None else "unknown"
        cwd = existing_session.cwd if existing_session is not None else "unknown"
        title = existing_session.title if existing_session is not None else None
        message_index = 0
        pending_codex_inline_media: list[PendingInlineMedia] = []
        messages: list[MessageRecord] = []
        for line_no, parsed in parsed_entries:
            if provider == "codex" and isinstance(parsed.payload, dict):
                inline_media = self._extract_codex_response_item_user_media(parsed.payload)
                if inline_media is not None:
                    pending_codex_inline_media.append(inline_media)
            if session_id is None and parsed.provider_session_id:
                session_id = parsed.provider_session_id
            if timestamp == "unknown" and parsed.timestamp:
                timestamp = parsed.timestamp
            if cwd == "unknown" and parsed.cwd:
                cwd = parsed.cwd
            for role, text in parsed.messages:
                if title is None and role == "user":
                    title = _compact_title(text)
                attachment_seeds: tuple[AttachmentSeed, ...] = ()
                if provider == "codex":
                    attachment_seeds = await self._resolve_codex_message_attachment_seeds(
                        parsed.payload,
                        role=role,
                        message_text=text,
                        pending_inline_media=pending_codex_inline_media,
                    )
                messages.append(
                    MessageRecord(
                        line_no=line_no,
                        message_index=message_index,
                        role=role,
                        content_text=text,
                        message_timestamp=parsed.timestamp,
                        attachment_seeds=attachment_seeds,
                    )
                )
                message_index += 1
        title_value = title or (f"session {session_id[:8]}" if isinstance(session_id, str) and session_id else "unknown session")
        return ProjectionSnapshot(
            session_id=session_id,
            timestamp=timestamp,
            cwd=cwd,
            title=title_value,
            first_seen_at=first_seen_at,
            messages=tuple(messages),
        )

    async def _apply_projection_snapshot(
        self,
        db: StorageSession,
        *,
        provider: AgentProvider,
        root_id: int,
        source_id: int,
        source_path: Path,
        projection: ProjectionSnapshot,
        op_prefix: str,
    ) -> None:
        await db.execute(
            "DELETE FROM session_messages WHERE source_id=?",
            (source_id,),
            op_name=f"{op_prefix}_delete_messages",
        )
        await db.execute(
            "DELETE FROM sessions WHERE source_id=?",
            (source_id,),
            op_name=f"{op_prefix}_delete_sessions",
        )
        session_row_id: Optional[int] = None
        if projection.session_id:
            await db.execute(
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
                (
                    root_id,
                    source_id,
                    provider,
                    projection.session_id,
                    projection.timestamp,
                    projection.cwd,
                    projection.title,
                    projection.first_seen_at,
                    _now_ts(),
                ),
                op_name=f"{op_prefix}_upsert_session",
            )
            session_row = await db.fetch_one(
                "SELECT id FROM sessions WHERE source_id=?",
                (source_id,),
                op_name=f"{op_prefix}_session_row_id",
            )
            assert session_row is not None
            session_row_id = int(session_row["id"])
        if session_row_id is not None:
            for message in projection.messages:
                message_row_id = await db.execute_insert(
                    """
                    INSERT INTO session_messages (
                        source_id, session_id, line_no, message_index, role, content_text, message_timestamp
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        source_id,
                        session_row_id,
                        message.line_no,
                        message.message_index,
                        message.role,
                        message.content_text,
                        message.message_timestamp,
                    ),
                    op_name=f"{op_prefix}_insert_session_message",
                )
                for attachment_index, seed in enumerate(message.attachment_seeds):
                    attachment_ref_id = await self.attachments.store_seed(db, seed, "codex_session_image")
                    if attachment_ref_id is None:
                        continue
                    await db.execute(
                        """
                        INSERT INTO session_message_attachments (message_id, attachment_index, attachment_ref_id)
                        VALUES (?, ?, ?)
                        """,
                        (message_row_id, attachment_index, attachment_ref_id),
                        op_name=f"{op_prefix}_insert_message_attachment",
                    )
        await db.execute(
            """
            UPDATE session_sources
            SET provider_session_id=?, source_path=?, updated_at=?
            WHERE id=?
            """,
            (projection.session_id, str(source_path), _now_ts(), source_id),
            op_name=f"{op_prefix}_update_source",
        )
        await self.attachments.gc_unreferenced(db)

    async def _mark_import_error(
        self,
        *,
        source_id: int,
        mtime_ns: int,
        size_bytes: int,
        started_at: int,
        error: str,
    ) -> None:
        async def _mark(db: StorageSession) -> None:
            await self._upsert_import_cursor(
                db,
                source_id=source_id,
                mtime_ns=mtime_ns,
                size_bytes=size_bytes,
                byte_offset=0,
                line_count=0,
                started_at=started_at,
                finished_at=_now_ts(),
                status="error",
                error=error,
            )

        await self.runtime.write(_mark)

    async def _mark_missing_sources_archived(
        self,
        db: StorageSession,
        root_id: int,
        seen_paths: set[str],
    ) -> None:
        rows = await db.fetch_all(
            "SELECT id, normalized_source_path FROM session_sources WHERE session_root_id=?",
            (root_id,),
            op_name="refresh_session_root_missing_rows",
        )
        now = _now_ts()
        for row in rows:
            normalized_path = str(row["normalized_source_path"])
            if normalized_path in seen_paths:
                continue
            await db.execute(
                """
                UPDATE session_sources
                SET source_status='archived_only', missing_since=?, updated_at=?
                WHERE id=?
                """,
                (now, now, int(row["id"])),
                op_name="mark_source_archived_only",
            )

    async def _mark_source_archived(self, db: StorageSession, root_id: int, normalized_path: str) -> None:
        now = _now_ts()
        await db.execute(
            """
            UPDATE session_sources
            SET source_status='archived_only', missing_since=?, updated_at=?
            WHERE session_root_id=? AND normalized_source_path=?
            """,
            (now, now, root_id, normalized_path),
            op_name="refresh_session_mark_archived",
        )

    async def _upsert_import_cursor(
        self,
        db: StorageSession,
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
        await db.execute(
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
            op_name="upsert_import_cursor",
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

    @staticmethod
    def _is_codex_image_wrapper_text(text: str) -> bool:
        normalized = text.strip()
        if not normalized:
            return True
        if normalized == "</image>":
            return True
        return normalized.startswith("<image ")

    def _extract_codex_response_item_user_media(self, payload: dict[str, Any]) -> Optional[PendingInlineMedia]:
        if str(payload.get("type") or "") != "response_item":
            return None
        body = payload.get("payload")
        if not isinstance(body, dict):
            return None
        if str(body.get("type") or "") != "message" or str(body.get("role") or "") != "user":
            return None
        content = body.get("content")
        if not isinstance(content, list):
            return None
        text_parts: list[str] = []
        attachments: list[AttachmentSeed] = []
        image_index = 0
        for item in content:
            if not isinstance(item, dict):
                continue
            item_type = str(item.get("type") or "")
            if item_type == "input_text":
                text = str(item.get("text") or "").strip()
                if text and not self._is_codex_image_wrapper_text(text):
                    text_parts.append(text)
                continue
            if item_type != "input_image":
                continue
            image_url = item.get("image_url")
            if not isinstance(image_url, str) or not image_url:
                continue
            image_index += 1
            fallback_name = f"codex-inline-image-{image_index}{extension_for_mime(self._parse_data_url_mime(image_url))}"
            seed = attachment_seed_from_data_url(image_url, fallback_name=fallback_name)
            if seed is not None:
                attachments.append(seed)
        if not attachments:
            return None
        return PendingInlineMedia(
            message_text="\n".join(part for part in text_parts if part).strip(),
            attachments=tuple(attachments),
        )

    def _extract_codex_event_message_attachments(self, payload: dict[str, Any]) -> tuple[AttachmentSeed, ...]:
        if str(payload.get("type") or "") != "event_msg":
            return ()
        body = payload.get("payload")
        if not isinstance(body, dict) or str(body.get("type") or "") != "user_message":
            return ()
        raw_local_images = body.get("local_images")
        if not isinstance(raw_local_images, list):
            return ()
        seeds: list[AttachmentSeed] = []
        for raw_local_image in raw_local_images:
            if not isinstance(raw_local_image, str) or not raw_local_image.strip():
                continue
            seed = attachment_seed_from_local_path(raw_local_image)
            if seed is not None:
                seeds.append(seed)
        return tuple(seeds)

    @staticmethod
    def _pop_codex_inline_media(
        pending_inline_media: list[PendingInlineMedia],
        message_text: str,
    ) -> Optional[PendingInlineMedia]:
        normalized = (message_text or "").strip()
        for index, entry in enumerate(pending_inline_media):
            if entry.message_text == normalized:
                return pending_inline_media.pop(index)
        if normalized:
            return None
        if pending_inline_media:
            return pending_inline_media.pop(0)
        return None

    async def _resolve_codex_message_attachment_seeds(
        self,
        payload: Optional[dict[str, Any]],
        *,
        role: str,
        message_text: str,
        pending_inline_media: list[PendingInlineMedia],
    ) -> tuple[AttachmentSeed, ...]:
        if role != "user" or not isinstance(payload, dict):
            return ()
        inline_media = self._pop_codex_inline_media(pending_inline_media, message_text)
        event_seeds = self._extract_codex_event_message_attachments(payload)
        attachment_seeds: tuple[AttachmentSeed, ...] = ()
        if any(seed.is_materializable() for seed in event_seeds):
            attachment_seeds = event_seeds
        elif inline_media is not None and inline_media.attachments:
            attachment_seeds = inline_media.attachments
        elif event_seeds:
            attachment_seeds = event_seeds
        prepared: list[AttachmentSeed] = []
        for seed in attachment_seeds:
            prepared_seed = await self.attachments.prepare_seed(seed)
            if not prepared_seed.is_materializable():
                continue
            prepared.append(prepared_seed)
        return tuple(prepared)

    def _parse_session_line(self, provider: AgentProvider, path: Path, raw_text: str) -> ParsedLine:
        if raw_text == "":
            return ParsedLine("empty", None, None, None, None, None, (), None)
        try:
            payload = json.loads(raw_text)
        except json.JSONDecodeError as exc:
            return ParsedLine("invalid_json", str(exc), None, None, None, None, (), None)
        if not isinstance(payload, dict):
            return ParsedLine("parsed", None, None, None, None, None, (), None)
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
                message = str(body.get("message") or "").strip()
                if msg_type == "user_message" and message:
                    messages = (("user", message),)
                elif msg_type == "agent_message" and message:
                    messages = (("assistant", message),)
            return ParsedLine("parsed", None, event_type, provider_session_id, timestamp, cwd, messages, payload)
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
        return ParsedLine("parsed", None, event_type, provider_session_id, timestamp, cwd, tuple(messages), payload)

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

    @staticmethod
    def _message_text_with_attachment_names(text: str, attachment_names: Sequence[str]) -> str:
        if not attachment_names:
            return text
        markers = [f"[图片: {name}]" for name in attachment_names if name]
        if not markers:
            return text
        parts = [text] if text else []
        parts.extend(markers)
        return "\n".join(parts)

    @staticmethod
    def _parse_data_url_mime(data_url: str) -> Optional[str]:
        if not isinstance(data_url, str) or not data_url.startswith("data:"):
            return None
        header, sep, _ = data_url.partition(",")
        if not sep:
            return None
        meta = header[5:]
        if not meta:
            return None
        parts = [part for part in meta.split(";") if part]
        if parts and "/" in parts[0]:
            return parts[0].strip().lower() or None
        return None
