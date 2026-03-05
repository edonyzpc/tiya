import json
from pathlib import Path

from tg_codex.services.state_store import StateStore


def test_state_store_provider_roundtrip(tmp_path: Path):
    path = tmp_path / "state.json"
    store = StateStore(path, default_provider="codex")

    store.set_active_provider(1001, "claude")
    store.set_active_session(1001, "sid-c", "/work-c", provider="claude")
    store.set_active_session(1001, "sid-x", "/work-x", provider="codex")
    store.set_last_session_ids(1001, ["sid-c", "sid-c2"], provider="claude")
    store.set_pending_session_pick(1001, True, provider="claude")

    store2 = StateStore(path, default_provider="codex")
    assert store2.get_active_provider(1001) == "claude"
    assert store2.get_active(1001, provider="claude") == ("sid-c", "/work-c")
    assert store2.get_active(1001, provider="codex") == ("sid-x", "/work-x")
    assert store2.get_last_session_ids(1001, provider="claude") == ["sid-c", "sid-c2"]
    assert store2.is_pending_session_pick(1001, provider="claude") is True


def test_legacy_schema_migrates_into_codex_bucket(tmp_path: Path):
    path = tmp_path / "state.json"
    payload = {
        "users": {
            "1": {
                "active_session_id": "legacy-session",
                "active_cwd": "/legacy",
                "last_session_ids": ["legacy-session", "legacy-2"],
                "pending_session_pick": True,
            }
        }
    }
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    store = StateStore(path, default_provider="claude")
    assert store.get_active_provider(1) == "codex"
    assert store.get_active(1, provider="codex") == ("legacy-session", "/legacy")
    assert store.get_last_session_ids(1, provider="codex") == ["legacy-session", "legacy-2"]
    assert store.is_pending_session_pick(1, provider="codex") is True


def test_clear_active_session_is_provider_scoped(tmp_path: Path):
    path = tmp_path / "state.json"
    store = StateStore(path, default_provider="codex")
    store.set_active_session(1, "sid-x", "/x", provider="codex")
    store.set_active_session(1, "sid-y", "/y", provider="claude")

    store.clear_active_session(1, "/new-y", provider="claude")

    assert store.get_active(1, provider="codex") == ("sid-x", "/x")
    assert store.get_active(1, provider="claude") == (None, "/new-y")
