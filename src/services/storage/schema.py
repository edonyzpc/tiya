from __future__ import annotations

from .runtime import StorageRuntime

SCHEMA_VERSION = 4
PARSER_VERSION = 4


def schema_sql() -> str:
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
            raw_blob BLOB NOT NULL,
            raw_codec TEXT NOT NULL,
            raw_sha256 TEXT NOT NULL,
            parse_status TEXT NOT NULL,
            parse_error TEXT,
            event_type TEXT,
            provider_session_id TEXT,
            event_timestamp TEXT,
            cwd TEXT,
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
            pending_image_path TEXT,
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
        CREATE TABLE IF NOT EXISTS session_message_attachments (
            message_id INTEGER NOT NULL,
            attachment_index INTEGER NOT NULL,
            attachment_ref_id INTEGER NOT NULL,
            PRIMARY KEY (message_id, attachment_index),
            FOREIGN KEY (message_id) REFERENCES session_messages(id) ON DELETE CASCADE,
            FOREIGN KEY (attachment_ref_id) REFERENCES attachment_refs(id) ON DELETE RESTRICT
        );
        CREATE TABLE IF NOT EXISTS run_attachments (
            run_id TEXT NOT NULL,
            attachment_index INTEGER NOT NULL,
            attachment_ref_id INTEGER NOT NULL,
            PRIMARY KEY (run_id, attachment_index),
            FOREIGN KEY (run_id) REFERENCES runs(run_id) ON DELETE CASCADE,
            FOREIGN KEY (attachment_ref_id) REFERENCES attachment_refs(id) ON DELETE RESTRICT
        );
    """


async def ensure_schema(runtime: StorageRuntime) -> None:
    async def _ensure(db) -> None:
        version = int(await db.fetch_value("PRAGMA user_version", op_name="schema_version", default=0) or 0)
        if version == SCHEMA_VERSION:
            return
        if version != 0:
            raise RuntimeError(
                f"storage schema {version} is not supported by this build; run `uv run storage rebuild`"
            )
        for statement in schema_sql().split(";\n"):
            sql = statement.strip()
            if not sql:
                continue
            await db.execute(sql, op_name="schema_statement")
        await db.execute(f"PRAGMA user_version={SCHEMA_VERSION}", op_name="schema_set_version")

    await runtime.write(_ensure)
