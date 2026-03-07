import asyncio
import base64
import hashlib
import json
import sqlite3
import zlib
from pathlib import Path

import pytest

from src.domain.models import ActiveRunState, InteractionOption, PendingInteraction
from src.services.session_store import ClaudeSessionStore, CodexSessionStore
from src.services.storage import StorageConfig, StorageManager
from src.services.storage.runtime import StorageRuntime
from src.services.storage.sessions import SessionStorage


def _write_codex_session(path: Path, session_id: str, cwd: str, user_text: str, assistant_text: str) -> None:
    lines = [
        {
            "type": "session_meta",
            "payload": {
                "id": session_id,
                "timestamp": "2026-03-05T00:00:00Z",
                "cwd": cwd,
            },
        },
        {
            "type": "event_msg",
            "payload": {
                "type": "user_message",
                "message": user_text,
            },
        },
        {
            "type": "event_msg",
            "payload": {
                "type": "agent_message",
                "message": assistant_text,
            },
        },
        "not-json-line",
        "",
    ]
    with path.open("w", encoding="utf-8") as f:
        for item in lines:
            if isinstance(item, str):
                f.write(item + "\n")
            else:
                f.write(json.dumps(item, ensure_ascii=False) + "\n")


def _write_claude_session(path: Path, cwd: str) -> None:
    lines = [
        {
            "type": "user",
            "sessionId": path.stem,
            "cwd": cwd,
            "timestamp": "2026-03-05T00:00:00Z",
            "isMeta": True,
            "message": {"role": "user", "content": "meta message"},
        },
        {
            "type": "user",
            "sessionId": path.stem,
            "cwd": cwd,
            "timestamp": "2026-03-05T00:00:01Z",
            "message": {"role": "user", "content": "how does claude session parsing work?"},
        },
        {
            "type": "assistant",
            "sessionId": path.stem,
            "cwd": cwd,
            "timestamp": "2026-03-05T00:00:02Z",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "reasoning"},
                    {"type": "text", "text": "assistant answer"},
                ],
            },
        },
    ]
    with path.open("w", encoding="utf-8") as f:
        for item in lines:
            f.write(json.dumps(item, ensure_ascii=False) + "\n")


@pytest.fixture
async def storage(tmp_path: Path):
    manager = await StorageManager.open(
        StorageConfig(
            db_path=tmp_path / "storage" / "tiya.db",
            instance_id="test-instance",
            attachments_root=tmp_path / "attachments",
        )
    )
    try:
        yield manager
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_codex_list_recent_and_title_extraction(storage: StorageManager, tmp_path: Path):
    root = tmp_path / "sessions"
    root.mkdir(parents=True)

    s1 = root / "a.jsonl"
    _write_codex_session(s1, "session-a", "/tmp/project-a", "first user question", "first answer")

    store = CodexSessionStore(root, storage)
    await store.refresh_recent()
    items = await store.list_recent(limit=5)

    assert len(items) == 1
    assert items[0].session_id == "session-a"
    assert "first user question" in items[0].title


@pytest.mark.asyncio
async def test_codex_get_history_with_limit_and_raw_lines(storage: StorageManager, tmp_path: Path):
    root = tmp_path / "sessions"
    root.mkdir(parents=True)

    s1 = root / "b.jsonl"
    _write_codex_session(s1, "session-b", "/tmp/project-b", "hello", "world")

    store = CodexSessionStore(root, storage)
    await store.refresh_recent()
    meta, history = await store.get_history("session-b", limit=1)

    assert meta is not None
    assert len(history) == 1
    role, message = history[0]
    assert role == "assistant"
    assert message == "world"

    stats = await storage.maintenance.stats()
    assert stats["table_counts"]["session_raw_lines"] == 5


@pytest.mark.asyncio
async def test_claude_list_recent_and_title_extraction(storage: StorageManager, tmp_path: Path):
    root = tmp_path / "claude"
    project_dir = root / "-mnt-code-tiya"
    project_dir.mkdir(parents=True)

    session_id = "925ffdad-54ba-41ed-8e3b-a7e72843079c"
    path = project_dir / f"{session_id}.jsonl"
    _write_claude_session(path, cwd="/mnt/code/tiya")

    store = ClaudeSessionStore(root, storage)
    await store.refresh_recent()
    items = await store.list_recent(limit=5)

    assert len(items) == 1
    assert items[0].session_id == session_id
    assert "how does claude session parsing work?" in items[0].title


@pytest.mark.asyncio
async def test_claude_get_history_ignores_meta_and_thinking(storage: StorageManager, tmp_path: Path):
    root = tmp_path / "claude"
    project_dir = root / "-mnt-code-tiya"
    project_dir.mkdir(parents=True)

    session_id = "bb8920d7-4fd8-4a1b-81ba-ea7444aa6b97"
    path = project_dir / f"{session_id}.jsonl"
    _write_claude_session(path, cwd="/mnt/code/tiya")

    subagent_dir = project_dir / "subagents"
    subagent_dir.mkdir(parents=True)
    (subagent_dir / "agent-1111111.jsonl").write_text("{}", encoding="utf-8")

    store = ClaudeSessionStore(root, storage)
    await store.refresh_recent()
    meta, history = await store.get_history(session_id, limit=10)

    assert meta is not None
    assert len(history) == 2
    assert history[0] == ("user", "how does claude session parsing work?")
    assert history[1] == ("assistant", "assistant answer")


@pytest.mark.asyncio
async def test_refresh_retries_after_import_error(storage: StorageManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    root = tmp_path / "sessions"
    root.mkdir(parents=True)

    path = root / "broken.jsonl"
    _write_codex_session(path, "session-broken", "/tmp/project-broken", "hello", "world")

    real_open = Path.open

    def _broken_open(self: Path, *args, **kwargs):
        mode = args[0] if args else kwargs.get("mode", "r")
        if self == path and mode == "rb":
            raise OSError("boom")
        return real_open(self, *args, **kwargs)

    store = CodexSessionStore(root, storage)
    monkeypatch.setattr(Path, "open", _broken_open)
    with pytest.raises(OSError, match="boom"):
        await store.refresh_recent()

    with sqlite3.connect(str(storage.db_path)) as conn:
        row = conn.execute(
            "SELECT last_status, last_error FROM session_import_cursors"
        ).fetchone()
    assert row == ("error", "OSError('boom')")

    monkeypatch.undo()
    await store.refresh_recent()
    items = await store.list_recent(limit=5)
    assert [item.session_id for item in items] == ["session-broken"]


@pytest.mark.asyncio
async def test_refresh_session_root_skips_disappearing_file(
    storage: StorageManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    root = tmp_path / "sessions"
    root.mkdir(parents=True)
    keep_path = root / "keep.jsonl"
    vanish_path = root / "vanish.jsonl"
    _write_codex_session(keep_path, "session-keep", "/tmp/project-keep", "hello", "world")
    _write_codex_session(vanish_path, "session-vanish", "/tmp/project-vanish", "bye", "later")

    real_build = SessionStorage._build_source_import_snapshot

    async def _disappear(self, provider, path, **kwargs):
        if path == vanish_path:
            path.unlink(missing_ok=True)
            raise FileNotFoundError(str(path))
        return await real_build(self, provider, path, **kwargs)

    monkeypatch.setattr(SessionStorage, "_build_source_import_snapshot", _disappear)

    store = CodexSessionStore(root, storage)
    await store.refresh_recent()
    items = await store.list_recent(limit=5)

    assert [item.session_id for item in items] == ["session-keep"]


@pytest.mark.asyncio
async def test_refresh_session_root_builds_file_snapshot_outside_db_callback(
    storage: StorageManager, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    root = tmp_path / "sessions"
    root.mkdir(parents=True)
    path = root / "outside-lock.jsonl"
    _write_codex_session(path, "session-outside-lock", "/tmp/project-outside", "hello", "world")

    snapshot_started = asyncio.Event()
    release_snapshot = asyncio.Event()
    real_build = SessionStorage._build_source_import_snapshot

    async def _blocked_build(self, provider, path, **kwargs):
        snapshot_started.set()
        await release_snapshot.wait()
        return await real_build(self, provider, path, **kwargs)

    monkeypatch.setattr(SessionStorage, "_build_source_import_snapshot", _blocked_build)

    refresh_task = asyncio.create_task(storage.sessions.refresh_session_root("codex", root))
    await asyncio.wait_for(snapshot_started.wait(), timeout=2)

    stats = await asyncio.wait_for(storage.maintenance.stats(), timeout=1)
    assert stats["table_counts"]["sessions"] == 0

    release_snapshot.set()
    await asyncio.wait_for(refresh_task, timeout=2)

    store = CodexSessionStore(root, storage)
    items = await store.list_recent(limit=5)
    assert [item.session_id for item in items] == ["session-outside-lock"]


@pytest.mark.asyncio
async def test_codex_history_includes_image_markers_and_stores_message_attachments(
    storage: StorageManager, tmp_path: Path
):
    root = tmp_path / "sessions"
    root.mkdir(parents=True)

    image_bytes = b"\x89PNG\r\n\x1a\ninline-image"
    image_data_url = "data:image/png;base64," + base64.b64encode(image_bytes).decode("ascii")
    path = root / "image-session.jsonl"
    lines = [
        {
            "type": "session_meta",
            "payload": {
                "id": "session-image",
                "timestamp": "2026-03-05T00:00:00Z",
                "cwd": "/tmp/project-image",
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [
                    {"type": "input_text", "text": "图片中宝可梦叫什么名字"},
                    {"type": "input_text", "text": "<image name=[Image #1]>"},
                    {"type": "input_image", "image_url": image_data_url},
                    {"type": "input_text", "text": "</image>"},
                ],
            },
        },
        {
            "type": "event_msg",
            "payload": {
                "type": "user_message",
                "message": "图片中宝可梦叫什么名字",
                "images": [],
                "local_images": [str(tmp_path / "missing-image.png")],
                "text_elements": [],
            },
        },
        {
            "type": "event_msg",
            "payload": {
                "type": "agent_message",
                "message": "看起来像是同人 Fakemon。",
            },
        },
    ]
    with path.open("w", encoding="utf-8") as f:
        for item in lines:
            f.write(json.dumps(item, ensure_ascii=False) + "\n")

    store = CodexSessionStore(root, storage)
    await store.refresh_recent()
    meta, history = await store.get_history("session-image", limit=10)

    assert meta is not None
    assert history[0][0] == "user"
    assert history[0][1] == "图片中宝可梦叫什么名字\n[图片: codex-inline-image-1.png]"
    assert history[1] == ("assistant", "看起来像是同人 Fakemon。")

    with sqlite3.connect(str(storage.db_path)) as conn:
        row = conn.execute(
            """
            SELECT ref.original_file_name
            FROM session_message_attachments sma
            JOIN attachment_refs ref ON ref.id=sma.attachment_ref_id
            """
        ).fetchone()
    assert row == ("codex-inline-image-1.png",)


@pytest.mark.asyncio
async def test_session_history_uses_sqlite_after_source_deleted(storage: StorageManager, tmp_path: Path):
    root = tmp_path / "sessions"
    root.mkdir(parents=True)

    path = root / "archived.jsonl"
    _write_codex_session(path, "session-archived", "/tmp/project-archived", "question", "answer")

    store = CodexSessionStore(root, storage)
    await store.refresh_recent()
    path.unlink()
    await store.refresh_recent()

    meta, history = await store.get_history("session-archived", limit=10)

    assert meta is not None
    assert history == [("user", "question"), ("assistant", "answer")]

    with sqlite3.connect(str(storage.db_path)) as conn:
        status = conn.execute("SELECT source_status FROM session_sources").fetchone()[0]
    assert status == "archived_only"


@pytest.mark.asyncio
async def test_session_raw_lines_store_original_bytes(storage: StorageManager, tmp_path: Path):
    root = tmp_path / "sessions"
    root.mkdir(parents=True)

    path = root / "archive-bytes.jsonl"
    _write_codex_session(path, "session-archive-bytes", "/tmp/project-archive", "hello", "world")
    expected_first_line = path.read_bytes().splitlines()[0]

    store = CodexSessionStore(root, storage)
    await store.refresh_recent()

    with sqlite3.connect(str(storage.db_path)) as conn:
        raw_blob, raw_codec, raw_sha256 = conn.execute(
            "SELECT raw_blob, raw_codec, raw_sha256 FROM session_raw_lines ORDER BY line_no ASC LIMIT 1"
        ).fetchone()

    payload = bytes(raw_blob)
    restored = zlib.decompress(payload) if raw_codec == "zlib" else payload
    assert restored == expected_first_line
    assert raw_sha256 == hashlib.sha256(expected_first_line).hexdigest()


@pytest.mark.asyncio
async def test_storage_rebuild_resets_runtime_state_and_reimports_sessions(tmp_path: Path):
    db_path = tmp_path / "storage" / "tiya.db"
    manager = await StorageManager.open(
        StorageConfig(
            db_path=db_path,
            instance_id="rebuild-test",
            attachments_root=tmp_path / "attachments",
            session_roots={"codex": tmp_path / "sessions"},
        )
    )
    root = tmp_path / "sessions"
    root.mkdir(parents=True, exist_ok=True)
    _write_codex_session(root / "rebuild.jsonl", "session-rebuild", "/tmp/project-rebuild", "hello", "world")

    await manager.sessions.refresh_session_root("codex", root)
    await manager.state.set_active_provider(7, "claude")
    await manager.state.set_active_session(7, "codex", "missing-session", "/work")
    await manager.state.set_active_run(
        7,
        "codex",
        ActiveRunState(run_id="run-1", chat_id=10, chat_type="private", started_at=1700000010),
    )
    await manager.state.set_pending_interaction(
        7,
        "codex",
        PendingInteraction(
            interaction_id="int-1",
            run_id="run-1",
            kind="question",
            title="confirm",
            body="body",
            options=(InteractionOption(id="yes", label="Yes"),),
            reply_mode="buttons",
            created_at=1700000011,
            expires_at=1700000111,
            chat_id=10,
            message_id=11,
        ),
    )
    await manager.close()

    rebuilt_path, backup_path = await StorageManager.rebuild_database(
        db_path=db_path,
        instance_id="rebuild-test",
        attachments_root=tmp_path / "attachments",
        session_roots={"codex": root},
    )

    assert rebuilt_path == db_path
    assert backup_path is not None
    assert backup_path.exists()

    rebuilt = await StorageManager.open(
        StorageConfig(
            db_path=db_path,
            instance_id="rebuild-test",
            attachments_root=tmp_path / "attachments",
            session_roots={"codex": root},
        )
    )
    try:
        assert await rebuilt.state.get_active_provider(7) == "codex"
        assert await rebuilt.state.get_active(7, "codex") == (None, None)
        active_run = await rebuilt.state.get_active_run(7, "codex")
        assert active_run is None
        interaction = await rebuilt.state.get_pending_interaction(7, "codex")
        assert interaction is None

        store = CodexSessionStore(root, rebuilt)
        items = await store.list_recent(limit=5)
        assert [item.session_id for item in items] == ["session-rebuild"]
    finally:
        await rebuilt.close()

    with sqlite3.connect(str(db_path)) as conn:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        source_status = conn.execute("SELECT source_status FROM session_sources").fetchone()[0]
        run_count = conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
        interaction_count = conn.execute("SELECT COUNT(*) FROM interactions").fetchone()[0]

    assert version == 4
    assert "session_message_attachments" in tables
    assert "run_attachments" in tables
    assert "session_events" not in tables
    assert source_status == "live"
    assert run_count == 0
    assert interaction_count == 0


@pytest.mark.asyncio
async def test_session_projection_can_rebuild_from_sqlite_archive(storage: StorageManager, tmp_path: Path):
    root = tmp_path / "sessions"
    root.mkdir(parents=True)

    image_bytes = b"\x89PNG\r\n\x1a\ninline-image"
    image_data_url = "data:image/png;base64," + base64.b64encode(image_bytes).decode("ascii")
    path = root / "archive-rebuild.jsonl"
    lines = [
        {
            "type": "session_meta",
            "payload": {
                "id": "session-archive-rebuild",
                "timestamp": "2026-03-05T00:00:00Z",
                "cwd": "/tmp/project-image",
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [
                    {"type": "input_text", "text": "图片里是什么"},
                    {"type": "input_text", "text": "<image name=[Image #1]>"},
                    {"type": "input_image", "image_url": image_data_url},
                    {"type": "input_text", "text": "</image>"},
                ],
            },
        },
        {
            "type": "event_msg",
            "payload": {
                "type": "user_message",
                "message": "图片里是什么",
                "images": [],
                "local_images": [str(tmp_path / "missing-image.png")],
                "text_elements": [],
            },
        },
        {
            "type": "event_msg",
            "payload": {
                "type": "agent_message",
                "message": "看起来是一张测试图片。",
            },
        },
    ]
    with path.open("w", encoding="utf-8") as f:
        for item in lines:
            f.write(json.dumps(item, ensure_ascii=False) + "\n")

    store = CodexSessionStore(root, storage)
    await store.refresh_recent()

    with sqlite3.connect(str(storage.db_path)) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("DELETE FROM session_message_attachments")
        conn.execute("DELETE FROM session_messages")
        conn.execute("DELETE FROM sessions")

    await storage.sessions.rebuild_session_projection("codex", root, "session-archive-rebuild")

    meta, history = await store.get_history("session-archive-rebuild", limit=10)
    assert meta is not None
    assert history == [
        ("user", "图片里是什么\n[图片: codex-inline-image-1.png]"),
        ("assistant", "看起来是一张测试图片。"),
    ]

    with sqlite3.connect(str(storage.db_path)) as conn:
        attachment_rows = conn.execute(
            """
            SELECT COUNT(*)
            FROM session_message_attachments
            """
        ).fetchone()[0]
        message_rows = conn.execute("SELECT COUNT(*) FROM session_messages").fetchone()[0]

    assert attachment_rows == 1
    assert message_rows == 2


@pytest.mark.asyncio
async def test_storage_rebuild_failure_keeps_existing_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db_path = tmp_path / "storage" / "tiya.db"
    root = tmp_path / "sessions"
    root.mkdir(parents=True)
    _write_codex_session(root / "keep.jsonl", "session-keep", "/tmp/project-keep", "hello", "world")

    manager = await StorageManager.open(
        StorageConfig(
            db_path=db_path,
            instance_id="rebuild-failure-test",
            attachments_root=tmp_path / "attachments",
            session_roots={"codex": root},
        )
    )
    await manager.sessions.refresh_session_root("codex", root)
    await manager.close()

    original_bytes = db_path.read_bytes()

    async def _boom(self, provider, root):
        raise RuntimeError(f"cannot import {root.name}")

    monkeypatch.setattr(SessionStorage, "refresh_session_root", _boom)
    with pytest.raises(RuntimeError, match="cannot import sessions"):
        await StorageManager.rebuild_database(
            db_path=db_path,
            instance_id="rebuild-failure-test",
            attachments_root=tmp_path / "attachments",
            session_roots={"codex": root},
        )

    assert db_path.exists()
    assert db_path.read_bytes() == original_bytes
    assert list(db_path.parent.glob("tiya.db.bak-*")) == []
    assert list(db_path.parent.glob("tiya.db.rebuild-*.tmp*")) == []


@pytest.mark.asyncio
async def test_storage_init_failure_is_repeatable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    async def _boom(self) -> None:
        raise sqlite3.OperationalError("broken runtime open")

    monkeypatch.setattr(StorageRuntime, "_open", _boom)
    config = StorageConfig(
        db_path=tmp_path / "storage" / "tiya.db",
        instance_id="broken-init",
        attachments_root=tmp_path / "attachments",
    )

    for _ in range(2):
        with pytest.raises(RuntimeError, match="failed to initialize sqlite storage") as excinfo:
            await StorageManager.open(config)
        assert isinstance(excinfo.value.__cause__, sqlite3.OperationalError)
        assert "broken runtime open" in str(excinfo.value.__cause__)
