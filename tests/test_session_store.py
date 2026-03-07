import base64
import json
import sqlite3
from pathlib import Path

import pytest

from src.services.session_store import ClaudeSessionStore, CodexSessionStore
from src.services.storage import StorageManager


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
    manager = StorageManager(
        db_path=tmp_path / "storage" / "tiya.db",
        instance_id="test-instance",
        attachments_root=tmp_path / "attachments",
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

    stats = await storage.stats()
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
async def test_storage_migrates_v1_db_to_v2(tmp_path: Path):
    db_path = tmp_path / "storage" / "tiya.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(str(db_path)) as conn:
        for statement in StorageManager._schema_v1_sql().split(";\n"):
            sql = statement.strip()
            if not sql:
                continue
            conn.execute(sql)
        conn.execute("PRAGMA user_version=1")

    manager = StorageManager(
        db_path=db_path,
        instance_id="migrate-test",
        attachments_root=tmp_path / "attachments",
    )
    await manager.close()

    with sqlite3.connect(str(db_path)) as conn:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}

    assert version == 2
    assert "session_message_attachments" in tables
    assert "run_attachments" in tables
