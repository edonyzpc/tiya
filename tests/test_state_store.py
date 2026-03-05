from pathlib import Path

from tg_codex.services.state_store import StateStore


def test_state_store_roundtrip(tmp_path: Path):
    path = tmp_path / "state.json"
    store = StateStore(path)

    store.set_active_session(1001, "sid-1", "/work")
    store.set_last_session_ids(1001, ["sid-1", "sid-2"])
    store.set_pending_session_pick(1001, True)

    store2 = StateStore(path)
    active_id, active_cwd = store2.get_active(1001)

    assert active_id == "sid-1"
    assert active_cwd == "/work"
    assert store2.get_last_session_ids(1001) == ["sid-1", "sid-2"]
    assert store2.is_pending_session_pick(1001) is True


def test_clear_active_session(tmp_path: Path):
    path = tmp_path / "state.json"
    store = StateStore(path)

    store.set_active_session(1, "sid-x", "/x")
    store.clear_active_session(1, "/y")

    active_id, active_cwd = store.get_active(1)
    assert active_id is None
    assert active_cwd == "/y"
