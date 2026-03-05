import json
from pathlib import Path

from tg_codex.services.session_store import ClaudeSessionStore, CodexSessionStore


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


def test_codex_list_recent_and_title_extraction(tmp_path: Path):
    root = tmp_path / "sessions"
    root.mkdir(parents=True)

    s1 = root / "a.jsonl"
    _write_codex_session(s1, "session-a", "/tmp/project-a", "first user question", "first answer")

    store = CodexSessionStore(root)
    items = store.list_recent(limit=5)

    assert len(items) == 1
    assert items[0].session_id == "session-a"
    assert "first user question" in items[0].title


def test_codex_get_history_with_limit(tmp_path: Path):
    root = tmp_path / "sessions"
    root.mkdir(parents=True)

    s1 = root / "b.jsonl"
    _write_codex_session(s1, "session-b", "/tmp/project-b", "hello", "world")

    store = CodexSessionStore(root)
    meta, history = store.get_history("session-b", limit=1)

    assert meta is not None
    assert len(history) == 1
    role, message = history[0]
    assert role == "assistant"
    assert message == "world"


def test_claude_list_recent_and_title_extraction(tmp_path: Path):
    root = tmp_path / "claude"
    project_dir = root / "-mnt-code-tiya"
    project_dir.mkdir(parents=True)

    session_id = "925ffdad-54ba-41ed-8e3b-a7e72843079c"
    path = project_dir / f"{session_id}.jsonl"
    _write_claude_session(path, cwd="/mnt/code/tiya")

    store = ClaudeSessionStore(root)
    items = store.list_recent(limit=5)

    assert len(items) == 1
    assert items[0].session_id == session_id
    assert "how does claude session parsing work?" in items[0].title


def test_claude_get_history_ignores_meta_and_thinking(tmp_path: Path):
    root = tmp_path / "claude"
    project_dir = root / "-mnt-code-tiya"
    project_dir.mkdir(parents=True)

    session_id = "bb8920d7-4fd8-4a1b-81ba-ea7444aa6b97"
    path = project_dir / f"{session_id}.jsonl"
    _write_claude_session(path, cwd="/mnt/code/tiya")

    # Should be ignored by list/get_history.
    subagent_dir = project_dir / "subagents"
    subagent_dir.mkdir(parents=True)
    (subagent_dir / "agent-1111111.jsonl").write_text("{}", encoding="utf-8")

    store = ClaudeSessionStore(root)
    meta, history = store.get_history(session_id, limit=10)

    assert meta is not None
    assert len(history) == 2
    assert history[0] == ("user", "how does claude session parsing work?")
    assert history[1] == ("assistant", "assistant answer")
