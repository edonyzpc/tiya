from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, Sequence

from ...domain.models import ActiveRunState, AgentProvider, PendingImage, PendingInteraction
from .attachments import AttachmentSeed, AttachmentStorage
from .runtime import StorageRuntime, StorageSession

_PROVIDERS: tuple[AgentProvider, AgentProvider] = ("codex", "claude")


def _now_ts() -> int:
    return int(time.time())


def _json_dumps(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


@dataclass(frozen=True)
class _PendingImageSnapshot:
    attachment_ref_id: Optional[int]
    image_path: Optional[str]
    file_name: str
    mime_type: Optional[str]
    file_size: Optional[int]
    message_id: int
    created_at: int


class StateStorage:
    def __init__(
        self,
        runtime: StorageRuntime,
        *,
        instance_id: str,
        default_provider: AgentProvider,
        attachments: AttachmentStorage,
        current_session_root_id: Callable[[AgentProvider], Optional[int]],
    ):
        self.runtime = runtime
        self.instance_id = instance_id
        self.default_provider = default_provider if default_provider in _PROVIDERS else "codex"
        self.attachments = attachments
        self.current_session_root_id = current_session_root_id

    async def ensure_instance(self) -> None:
        now = _now_ts()

        async def _ensure(db: StorageSession) -> None:
            await db.execute(
                """
                INSERT INTO instances (instance_id, created_at, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(instance_id) DO UPDATE SET updated_at=excluded.updated_at
                """,
                (self.instance_id, now, now),
                op_name="ensure_instance",
            )

        await self.runtime.write(_ensure)

    async def recover_instance_state(self) -> None:
        now = _now_ts()

        async def _recover(db: StorageSession) -> None:
            await db.execute(
                """
                UPDATE runs
                SET status='aborted_on_boot', finished_at=COALESCE(finished_at, ?)
                WHERE instance_id=? AND status='running'
                """,
                (now, self.instance_id),
                op_name="recover_runs",
            )
            await db.execute(
                """
                UPDATE interactions
                SET status='expired_on_boot'
                WHERE instance_id=? AND status='pending'
                """,
                (self.instance_id,),
                op_name="recover_interactions",
            )
            rows = await db.fetch_all(
                "SELECT telegram_user_id, provider, session_root_id FROM provider_state WHERE instance_id=?",
                (self.instance_id,),
                op_name="recover_provider_rows",
            )
            for row in rows:
                provider = str(row["provider"])
                current_root_id = self.current_session_root_id(provider) if provider in _PROVIDERS else None
                user_id = int(row["telegram_user_id"])
                if (
                    current_root_id is not None
                    and row["session_root_id"] is not None
                    and int(row["session_root_id"]) != current_root_id
                ):
                    await db.execute(
                        """
                        UPDATE provider_state
                        SET active_session_id=NULL,
                            session_root_id=?,
                            active_run_id=NULL,
                            pending_interaction_id=NULL
                        WHERE instance_id=? AND telegram_user_id=? AND provider=?
                        """,
                        (current_root_id, self.instance_id, user_id, provider),
                        op_name="recover_provider_root_reset",
                    )
                    await db.execute(
                        """
                        DELETE FROM session_pick_cache
                        WHERE instance_id=? AND telegram_user_id=? AND provider=?
                        """,
                        (self.instance_id, user_id, provider),
                        op_name="recover_provider_cache_reset",
                    )
                    continue
                await db.execute(
                    """
                    UPDATE provider_state
                    SET active_run_id=NULL,
                        pending_interaction_id=NULL,
                        session_root_id=COALESCE(?, session_root_id)
                    WHERE instance_id=? AND telegram_user_id=? AND provider=?
                    """,
                    (current_root_id, self.instance_id, user_id, provider),
                    op_name="recover_provider_state",
                )

        await self.runtime.write(_recover)

    async def get_active_provider(self, user_id: int) -> AgentProvider:
        async def _get(db: StorageSession):
            return await db.fetch_one(
                "SELECT active_provider FROM user_state WHERE telegram_user_id=?",
                (user_id,),
                op_name="get_active_provider",
            )

        row = await self.runtime.read(_get)
        if row is None:
            return self.default_provider
        provider = str(row["active_provider"])
        if provider in _PROVIDERS:
            return provider
        return self.default_provider

    async def set_active_provider(self, user_id: int, provider: AgentProvider) -> None:
        async def _set(db: StorageSession) -> None:
            await db.execute(
                """
                INSERT INTO user_state (telegram_user_id, active_provider)
                VALUES (?, ?)
                ON CONFLICT(telegram_user_id) DO UPDATE SET active_provider=excluded.active_provider
                """,
                (user_id, provider),
                op_name="set_active_provider",
            )

        await self.runtime.write(_set)

    async def set_active_session(self, user_id: int, provider: AgentProvider, session_id: str, cwd: str) -> None:
        async def _set(db: StorageSession) -> None:
            await self._ensure_provider_state_tx(db, user_id, provider)
            await db.execute(
                """
                UPDATE provider_state
                SET session_root_id=?,
                    active_session_id=?,
                    active_cwd=?
                WHERE instance_id=? AND telegram_user_id=? AND provider=?
                """,
                (self.current_session_root_id(provider), session_id, cwd, self.instance_id, user_id, provider),
                op_name="set_active_session",
            )

        await self.runtime.write(_set)

    async def clear_active_session(self, user_id: int, provider: AgentProvider, cwd: str) -> None:
        async def _clear(db: StorageSession) -> None:
            await self._ensure_provider_state_tx(db, user_id, provider)
            await db.execute(
                """
                UPDATE provider_state
                SET session_root_id=?,
                    active_session_id=NULL,
                    active_cwd=?
                WHERE instance_id=? AND telegram_user_id=? AND provider=?
                """,
                (self.current_session_root_id(provider), cwd, self.instance_id, user_id, provider),
                op_name="clear_active_session",
            )

        await self.runtime.write(_clear)

    async def get_active(self, user_id: int, provider: AgentProvider) -> tuple[Optional[str], Optional[str]]:
        async def _get(db: StorageSession):
            return await db.fetch_one(
                """
                SELECT active_session_id, active_cwd
                FROM provider_state
                WHERE instance_id=? AND telegram_user_id=? AND provider=?
                """,
                (self.instance_id, user_id, provider),
                op_name="get_active",
            )

        row = await self.runtime.read(_get)
        if row is None:
            return None, None
        session_id = row["active_session_id"]
        cwd = row["active_cwd"]
        return (session_id if isinstance(session_id, str) else None, cwd if isinstance(cwd, str) else None)

    async def set_last_session_ids(self, user_id: int, provider: AgentProvider, session_ids: Sequence[str]) -> None:
        normalized_ids = [str(value) for value in session_ids if str(value).strip()]

        async def _set(db: StorageSession) -> None:
            await self._ensure_provider_state_tx(db, user_id, provider)
            await db.execute(
                "DELETE FROM session_pick_cache WHERE instance_id=? AND telegram_user_id=? AND provider=?",
                (self.instance_id, user_id, provider),
                op_name="clear_last_session_ids",
            )
            for index, session_id in enumerate(normalized_ids, start=1):
                await db.execute(
                    """
                    INSERT INTO session_pick_cache (instance_id, telegram_user_id, provider, position, session_id)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (self.instance_id, user_id, provider, index, session_id),
                    op_name="insert_last_session_id",
                )

        await self.runtime.write(_set)

    async def get_last_session_ids(self, user_id: int, provider: AgentProvider) -> list[str]:
        async def _get(db: StorageSession):
            return await db.fetch_all(
                """
                SELECT session_id
                FROM session_pick_cache
                WHERE instance_id=? AND telegram_user_id=? AND provider=?
                ORDER BY position ASC
                """,
                (self.instance_id, user_id, provider),
                op_name="get_last_session_ids",
            )

        rows = await self.runtime.read(_get)
        return [str(row["session_id"]) for row in rows]

    async def set_pending_session_pick(self, user_id: int, provider: AgentProvider, enabled: bool) -> None:
        async def _set(db: StorageSession) -> None:
            await self._ensure_provider_state_tx(db, user_id, provider)
            await db.execute(
                """
                UPDATE provider_state
                SET pending_session_pick=?
                WHERE instance_id=? AND telegram_user_id=? AND provider=?
                """,
                (1 if enabled else 0, self.instance_id, user_id, provider),
                op_name="set_pending_session_pick",
            )

        await self.runtime.write(_set)

    async def is_pending_session_pick(self, user_id: int, provider: AgentProvider) -> bool:
        async def _get(db: StorageSession):
            return await db.fetch_one(
                """
                SELECT pending_session_pick
                FROM provider_state
                WHERE instance_id=? AND telegram_user_id=? AND provider=?
                """,
                (self.instance_id, user_id, provider),
                op_name="get_pending_session_pick",
            )

        row = await self.runtime.read(_get)
        return bool(row["pending_session_pick"]) if row is not None else False

    async def set_pending_image(self, user_id: int, provider: AgentProvider, image: PendingImage) -> None:
        prepared_seed: Optional[AttachmentSeed] = None
        if image.attachment_ref_id is None and image.path.is_file():
            prepared_seed = await self.attachments.prepare_seed(
                AttachmentSeed(
                    path=image.path,
                    file_name=image.file_name,
                    mime_type=image.mime_type,
                    file_size=image.file_size,
                )
            )

        async def _set(db: StorageSession) -> None:
            await self._ensure_provider_state_tx(db, user_id, provider)
            attachment_ref_id = image.attachment_ref_id
            image_path: Optional[str] = None
            if attachment_ref_id is None and prepared_seed is not None:
                attachment_ref_id = await self.attachments.store_seed(db, prepared_seed, "pending_image")
            if attachment_ref_id is None:
                image_path = str(image.path)
            await db.execute(
                """
                UPDATE provider_state
                SET pending_attachment_ref_id=?,
                    pending_image_path=?,
                    pending_image_file_name=?,
                    pending_image_mime_type=?,
                    pending_image_file_size=?,
                    pending_image_message_id=?,
                    pending_image_created_at=?
                WHERE instance_id=? AND telegram_user_id=? AND provider=?
                """,
                (
                    attachment_ref_id,
                    image_path,
                    image.file_name,
                    image.mime_type,
                    image.file_size,
                    image.message_id,
                    image.created_at,
                    self.instance_id,
                    user_id,
                    provider,
                ),
                op_name="set_pending_image",
            )

        await self.runtime.write(_set)

    async def get_pending_image(self, user_id: int, provider: AgentProvider) -> Optional[PendingImage]:
        loaded = await self.runtime.read(lambda db: self._load_pending_image(db, user_id, provider))
        return self._materialize_pending_image(user_id, provider, loaded)

    async def clear_pending_image(self, user_id: int, provider: AgentProvider) -> Optional[PendingImage]:
        loaded = await self.runtime.read(lambda db: self._load_pending_image(db, user_id, provider))
        pending = self._materialize_pending_image(user_id, provider, loaded)
        if loaded is None or pending is None:
            return None
        snapshot, _payload = loaded

        async def _clear(db: StorageSession) -> None:
            row = await self._read_pending_image_row(db, user_id, provider)
            current = self._pending_image_snapshot_from_row(row) if row is not None else None
            if current != snapshot:
                return
            await db.execute(
                """
                UPDATE provider_state
                SET pending_attachment_ref_id=NULL,
                    pending_image_path=NULL,
                    pending_image_file_name=NULL,
                    pending_image_mime_type=NULL,
                    pending_image_file_size=NULL,
                    pending_image_message_id=NULL,
                    pending_image_created_at=NULL
                WHERE instance_id=? AND telegram_user_id=? AND provider=?
                """,
                (self.instance_id, user_id, provider),
                op_name="clear_pending_image",
            )

        await self.runtime.write(_clear)
        return pending

    async def set_active_run(
        self,
        user_id: int,
        provider: AgentProvider,
        active_run: Optional[ActiveRunState],
    ) -> None:
        async def _set(db: StorageSession) -> None:
            await self._ensure_provider_state_tx(db, user_id, provider)
            if active_run is None:
                await db.execute(
                    """
                    UPDATE provider_state
                    SET active_run_id=NULL
                    WHERE instance_id=? AND telegram_user_id=? AND provider=?
                    """,
                    (self.instance_id, user_id, provider),
                    op_name="clear_active_run_ref",
                )
                return
            await db.execute(
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
                op_name="upsert_active_run",
            )
            await db.execute(
                """
                UPDATE provider_state
                SET active_run_id=?
                WHERE instance_id=? AND telegram_user_id=? AND provider=?
                """,
                (active_run.run_id, self.instance_id, user_id, provider),
                op_name="set_active_run_ref",
            )

        await self.runtime.write(_set)

    async def get_active_run(self, user_id: int, provider: AgentProvider) -> Optional[ActiveRunState]:
        async def _get(db: StorageSession):
            return await db.fetch_one(
                """
                SELECT run_row.run_id, run_row.chat_id, run_row.chat_type, run_row.started_at
                FROM provider_state state
                JOIN runs run_row ON run_row.run_id=state.active_run_id
                WHERE state.instance_id=? AND state.telegram_user_id=? AND state.provider=?
                """,
                (self.instance_id, user_id, provider),
                op_name="get_active_run",
            )

        row = await self.runtime.read(_get)
        if row is None:
            return None
        return ActiveRunState.from_dict(
            {
                "run_id": row["run_id"],
                "chat_id": row["chat_id"],
                "chat_type": row["chat_type"],
                "started_at": row["started_at"],
            }
        )

    async def clear_active_run(self, user_id: int, provider: AgentProvider) -> Optional[ActiveRunState]:
        async def _clear(db: StorageSession) -> Optional[dict[str, object]]:
            row = await db.fetch_one(
                """
                SELECT run_row.run_id, run_row.chat_id, run_row.chat_type, run_row.started_at
                FROM provider_state state
                JOIN runs run_row ON run_row.run_id=state.active_run_id
                WHERE state.instance_id=? AND state.telegram_user_id=? AND state.provider=?
                """,
                (self.instance_id, user_id, provider),
                op_name="read_active_run_for_clear",
            )
            await db.execute(
                """
                UPDATE provider_state
                SET active_run_id=NULL
                WHERE instance_id=? AND telegram_user_id=? AND provider=?
                """,
                (self.instance_id, user_id, provider),
                op_name="clear_active_run",
            )
            if row is None:
                return None
            return {
                "run_id": row["run_id"],
                "chat_id": row["chat_id"],
                "chat_type": row["chat_type"],
                "started_at": row["started_at"],
            }

        payload = await self.runtime.write(_clear)
        return ActiveRunState.from_dict(payload) if payload is not None else None

    async def set_pending_interaction(
        self,
        user_id: int,
        provider: AgentProvider,
        interaction: Optional[PendingInteraction],
    ) -> None:
        async def _set(db: StorageSession) -> None:
            await self._ensure_provider_state_tx(db, user_id, provider)
            if interaction is None:
                await db.execute(
                    """
                    UPDATE provider_state
                    SET pending_interaction_id=NULL
                    WHERE instance_id=? AND telegram_user_id=? AND provider=?
                    """,
                    (self.instance_id, user_id, provider),
                    op_name="clear_pending_interaction_ref",
                )
                return
            await db.execute(
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
                    _json_dumps(
                        [
                            {
                                "id": option.id,
                                "label": option.label,
                                "description": option.description,
                            }
                            for option in interaction.options
                        ]
                    ),
                    interaction.reply_mode,
                    interaction.created_at,
                    interaction.expires_at,
                    interaction.chat_id,
                    interaction.message_id,
                ),
                op_name="upsert_pending_interaction",
            )
            await db.execute(
                """
                UPDATE provider_state
                SET pending_interaction_id=?
                WHERE instance_id=? AND telegram_user_id=? AND provider=?
                """,
                (interaction.interaction_id, self.instance_id, user_id, provider),
                op_name="set_pending_interaction_ref",
            )

        await self.runtime.write(_set)

    async def get_pending_interaction(self, user_id: int, provider: AgentProvider) -> Optional[PendingInteraction]:
        async def _get(db: StorageSession):
            return await db.fetch_one(
                """
                SELECT interaction.*
                FROM provider_state state
                JOIN interactions interaction ON interaction.interaction_id=state.pending_interaction_id
                WHERE state.instance_id=? AND state.telegram_user_id=? AND state.provider=?
                """,
                (self.instance_id, user_id, provider),
                op_name="get_pending_interaction",
            )

        row = await self.runtime.read(_get)
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

    async def clear_pending_interaction(self, user_id: int, provider: AgentProvider) -> Optional[PendingInteraction]:
        async def _clear(db: StorageSession) -> Optional[dict[str, object]]:
            row = await db.fetch_one(
                """
                SELECT interaction.*
                FROM provider_state state
                JOIN interactions interaction ON interaction.interaction_id=state.pending_interaction_id
                WHERE state.instance_id=? AND state.telegram_user_id=? AND state.provider=?
                """,
                (self.instance_id, user_id, provider),
                op_name="read_pending_interaction_for_clear",
            )
            await db.execute(
                """
                UPDATE provider_state
                SET pending_interaction_id=NULL
                WHERE instance_id=? AND telegram_user_id=? AND provider=?
                """,
                (self.instance_id, user_id, provider),
                op_name="clear_pending_interaction",
            )
            if row is None:
                return None
            return {
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

        payload = await self.runtime.write(_clear)
        return PendingInteraction.from_dict(payload) if payload is not None else None

    async def record_interaction_result(self, interaction_id: str, status: str) -> None:
        async def _record(db: StorageSession) -> None:
            await db.execute(
                "UPDATE interactions SET status=? WHERE interaction_id=?",
                (status, interaction_id),
                op_name="record_interaction_result",
            )

        await self.runtime.write(_record)

    async def record_run_result(
        self,
        *,
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
        attachment_ref_ids: tuple[int, ...],
    ) -> None:
        async def _record(db: StorageSession) -> None:
            await db.execute(
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
                    self._sha256_text(prompt),
                    len(answer),
                    self._sha256_text(answer),
                    len(stderr_text),
                    self._sha256_text(stderr_text),
                    return_code,
                    run_id,
                    self.instance_id,
                    user_id,
                    provider,
                ),
                op_name="record_run_result",
            )
            await db.execute("DELETE FROM run_attachments WHERE run_id=?", (run_id,), op_name="clear_run_attachments")
            for attachment_index, attachment_ref_id in enumerate(attachment_ref_ids):
                await db.execute(
                    """
                    INSERT INTO run_attachments (run_id, attachment_index, attachment_ref_id)
                    VALUES (?, ?, ?)
                    """,
                    (run_id, attachment_index, attachment_ref_id),
                    op_name="insert_run_attachment",
                )

        await self.runtime.write(_record)

    async def _ensure_provider_state_tx(self, db: StorageSession, user_id: int, provider: AgentProvider) -> None:
        await db.execute(
            """
            INSERT INTO user_state (telegram_user_id, active_provider)
            VALUES (?, ?)
            ON CONFLICT(telegram_user_id) DO NOTHING
            """,
            (user_id, self.default_provider),
            op_name="ensure_user_state",
        )
        await db.execute(
            """
            INSERT INTO provider_state (instance_id, telegram_user_id, provider, session_root_id)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(instance_id, telegram_user_id, provider) DO UPDATE SET
                session_root_id=COALESCE(excluded.session_root_id, provider_state.session_root_id)
            """,
            (self.instance_id, user_id, provider, self.current_session_root_id(provider)),
            op_name="ensure_provider_state",
        )

    async def _read_pending_image_row(
        self,
        db: StorageSession,
        user_id: int,
        provider: AgentProvider,
    ):
        return await db.fetch_one(
            """
            SELECT *
            FROM provider_state
            WHERE instance_id=? AND telegram_user_id=? AND provider=?
            """,
            (self.instance_id, user_id, provider),
            op_name="read_pending_image_row",
        )

    async def _load_pending_image(
        self,
        db: StorageSession,
        user_id: int,
        provider: AgentProvider,
    ) -> Optional[tuple[_PendingImageSnapshot, Optional[tuple[str, bytes]]]]:
        row = await self._read_pending_image_row(db, user_id, provider)
        snapshot = self._pending_image_snapshot_from_row(row) if row is not None else None
        if snapshot is None:
            return None
        payload: Optional[tuple[str, bytes]] = None
        if snapshot.attachment_ref_id is not None:
            payload = await self.attachments.read_ref_payload_from_db(db, snapshot.attachment_ref_id)
        return snapshot, payload

    def _pending_image_snapshot_from_row(self, row) -> Optional[_PendingImageSnapshot]:
        file_name = row["pending_image_file_name"]
        message_id = row["pending_image_message_id"]
        created_at = row["pending_image_created_at"]
        if not (isinstance(file_name, str) and isinstance(message_id, int) and isinstance(created_at, int)):
            return None
        attachment_ref_id = row["pending_attachment_ref_id"]
        image_path = row["pending_image_path"]
        if not isinstance(attachment_ref_id, int) and not (isinstance(image_path, str) and image_path):
            return None
        return _PendingImageSnapshot(
            attachment_ref_id=int(attachment_ref_id) if isinstance(attachment_ref_id, int) else None,
            image_path=str(image_path) if isinstance(image_path, str) else None,
            file_name=file_name,
            mime_type=row["pending_image_mime_type"] if isinstance(row["pending_image_mime_type"], str) else None,
            file_size=row["pending_image_file_size"] if isinstance(row["pending_image_file_size"], int) else None,
            message_id=message_id,
            created_at=created_at,
        )

    def _materialize_pending_image(
        self,
        user_id: int,
        provider: AgentProvider,
        loaded: Optional[tuple[_PendingImageSnapshot, Optional[tuple[str, bytes]]]],
    ) -> Optional[PendingImage]:
        if loaded is None:
            return None
        snapshot, payload = loaded
        if payload is not None and snapshot.attachment_ref_id is not None:
            original_file_name, data = payload
            path = self.attachments.materialize_payload(
                snapshot.attachment_ref_id,
                original_file_name=original_file_name,
                data=data,
                user_id=user_id,
                provider=provider,
                file_name=snapshot.file_name,
            )
        else:
            image_path = snapshot.image_path
            if not isinstance(image_path, str) or not image_path:
                return None
            path = Path(image_path)
        return PendingImage(
            path=path,
            file_name=snapshot.file_name,
            mime_type=snapshot.mime_type,
            file_size=snapshot.file_size,
            message_id=snapshot.message_id,
            created_at=snapshot.created_at,
            attachment_ref_id=snapshot.attachment_ref_id,
        )

    @staticmethod
    def _sha256_text(value: str) -> str:
        import hashlib

        return hashlib.sha256(value.encode("utf-8")).hexdigest()
