import json
from pathlib import Path

from tg_codex.services.session_store import SessionStore


def _write_session(path: Path, session_id: str, cwd: str, user_text: str, assistant_text: str) -> None:
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
    ]
    with path.open("w", encoding="utf-8") as f:
        for item in lines:
            if isinstance(item, str):
                f.write(item + "\n")
            else:
                f.write(json.dumps(item, ensure_ascii=False) + "\n")


def test_list_recent_and_title_extraction(tmp_path: Path):
    root = tmp_path / "sessions"
    root.mkdir(parents=True)

    s1 = root / "a.jsonl"
    _write_session(s1, "session-a", "/tmp/project-a", "first user question", "first answer")

    store = SessionStore(root)
    items = store.list_recent(limit=5)

    assert len(items) == 1
    assert items[0].session_id == "session-a"
    assert "first user question" in items[0].title


def test_get_history_with_limit(tmp_path: Path):
    root = tmp_path / "sessions"
    root.mkdir(parents=True)

    s1 = root / "b.jsonl"
    _write_session(s1, "session-b", "/tmp/project-b", "hello", "world")

    store = SessionStore(root)
    meta, history = store.get_history("session-b", limit=1)

    assert meta is not None
    assert len(history) == 1
    role, message = history[0]
    assert role == "assistant"
    assert message == "world"
