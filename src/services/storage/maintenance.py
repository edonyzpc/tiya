from __future__ import annotations

import time
from pathlib import Path

from .attachments import AttachmentStorage
from .runtime import StorageRuntime

CONFIG_SNAPSHOT_RETENTION = 32
_STATS_COUNT_QUERIES: dict[str, str] = {
    "sessions": "SELECT COUNT(*) FROM sessions",
    "session_raw_lines": "SELECT COUNT(*) FROM session_raw_lines",
    "session_messages": "SELECT COUNT(*) FROM session_messages",
    "session_message_attachments": "SELECT COUNT(*) FROM session_message_attachments",
    "attachment_blobs": "SELECT COUNT(*) FROM attachment_blobs",
    "attachment_refs": "SELECT COUNT(*) FROM attachment_refs",
    "runs": "SELECT COUNT(*) FROM runs",
    "run_attachments": "SELECT COUNT(*) FROM run_attachments",
    "interactions": "SELECT COUNT(*) FROM interactions",
}


def _now_ts() -> int:
    return int(time.time())


class MaintenanceStorage:
    def __init__(self, runtime: StorageRuntime, db_path: Path, attachments: AttachmentStorage):
        self.runtime = runtime
        self.db_path = db_path
        self.attachments = attachments

    async def snapshot_config(self, instance_id: str, payload: dict[str, object]) -> None:
        import json

        async def _snapshot(db) -> None:
            await db.execute(
                "INSERT INTO config_snapshots (instance_id, created_at, snapshot_json) VALUES (?, ?, ?)",
                (instance_id, _now_ts(), json.dumps(payload, ensure_ascii=False, separators=(",", ":"))),
                op_name="insert_config_snapshot",
            )
            await db.execute(
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
                (instance_id, instance_id, CONFIG_SNAPSHOT_RETENTION),
                op_name="trim_config_snapshots",
            )

        await self.runtime.write(_snapshot)

    async def backup(self, destination: Path) -> None:
        await self.runtime.backup(destination)

    async def checkpoint_truncate(self) -> None:
        await self.runtime.checkpoint_truncate()

    async def vacuum(self) -> None:
        await self.runtime.vacuum()

    async def stats(self) -> dict[str, object]:
        async def _stats(db) -> dict[str, object]:
            table_counts: dict[str, int] = {}
            for table_name, query in _STATS_COUNT_QUERIES.items():
                table_counts[table_name] = int(await db.fetch_value(query, op_name=f"stats_{table_name}", default=0) or 0)
            page_count = int(await db.fetch_value("PRAGMA page_count", op_name="stats_page_count", default=0) or 0)
            page_size = int(await db.fetch_value("PRAGMA page_size", op_name="stats_page_size", default=0) or 0)
            session_archive_bytes = int(
                await db.fetch_value(
                    "SELECT COALESCE(SUM(LENGTH(raw_blob)), 0) FROM session_raw_lines",
                    op_name="stats_session_archive_bytes",
                    default=0,
                )
                or 0
            )
            session_projection_bytes = int(
                await db.fetch_value(
                    "SELECT COALESCE(SUM(LENGTH(content_text)), 0) FROM session_messages",
                    op_name="stats_session_projection_bytes",
                    default=0,
                )
                or 0
            )
            return {
                "approx_size_bytes": page_count * page_size,
                "session_archive_bytes": session_archive_bytes,
                "session_projection_bytes_estimate": session_projection_bytes,
                "table_counts": table_counts,
            }

        snapshot = await self.runtime.read(_stats)
        wal_path = self.db_path.with_name(self.db_path.name + "-wal")
        db_file_bytes = self.db_path.stat().st_size if self.db_path.exists() else 0
        wal_file_bytes = wal_path.stat().st_size if wal_path.exists() else 0
        return {
            "db_path": str(self.db_path),
            "approx_size_bytes": int(snapshot["approx_size_bytes"]),
            "db_file_bytes": db_file_bytes,
            "wal_file_bytes": wal_file_bytes,
            "session_archive_bytes": int(snapshot["session_archive_bytes"]),
            "session_projection_bytes_estimate": int(snapshot["session_projection_bytes_estimate"]),
            "table_counts": dict(snapshot["table_counts"]),
        }
