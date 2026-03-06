import asyncio
import json
from pathlib import Path

import pytest

from src.services.state_store import SCHEMA_VERSION, StateStore


@pytest.mark.asyncio
async def test_state_store_provider_roundtrip(tmp_path: Path):
    path = tmp_path / "state.json"
    store = StateStore(path, default_provider="codex", flush_delay_sec=0.01)

    await store.set_active_provider(1001, "claude")
    await store.set_active_session(1001, "sid-c", "/work-c", provider="claude")
    await store.set_active_session(1001, "sid-x", "/work-x", provider="codex")
    await store.set_last_session_ids(1001, ["sid-c", "sid-c2"], provider="claude")
    await store.set_pending_session_pick(1001, True, provider="claude")
    await store.close()

    store2 = StateStore(path, default_provider="codex")
    assert await store2.get_active_provider(1001) == "claude"
    assert await store2.get_active(1001, provider="claude") == ("sid-c", "/work-c")
    assert await store2.get_active(1001, provider="codex") == ("sid-x", "/work-x")
    assert await store2.get_last_session_ids(1001, provider="claude") == ["sid-c", "sid-c2"]
    assert await store2.is_pending_session_pick(1001, provider="claude") is True
    await store2.close()


@pytest.mark.asyncio
async def test_legacy_schema_migrates_into_codex_bucket(tmp_path: Path):
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
    assert await store.get_active_provider(1) == "codex"
    assert await store.get_active(1, provider="codex") == ("legacy-session", "/legacy")
    assert await store.get_last_session_ids(1, provider="codex") == ["legacy-session", "legacy-2"]
    assert await store.is_pending_session_pick(1, provider="codex") is True
    assert json.loads(path.read_text(encoding="utf-8"))["schema_version"] == SCHEMA_VERSION
    await store.close()


@pytest.mark.asyncio
async def test_clear_active_session_is_provider_scoped(tmp_path: Path):
    path = tmp_path / "state.json"
    store = StateStore(path, default_provider="codex", flush_delay_sec=0.01)
    await store.set_active_session(1, "sid-x", "/x", provider="codex")
    await store.set_active_session(1, "sid-y", "/y", provider="claude")

    await store.clear_active_session(1, "/new-y", provider="claude")

    assert await store.get_active(1, provider="codex") == ("sid-x", "/x")
    assert await store.get_active(1, provider="claude") == (None, "/new-y")
    await store.close()


@pytest.mark.asyncio
async def test_state_store_concurrent_writes_do_not_drop_state(tmp_path: Path):
    path = tmp_path / "state.json"
    store = StateStore(path, default_provider="codex", flush_delay_sec=0.01)

    async def _write(idx: int) -> None:
        await store.set_active_session(idx, f"sid-{idx}", f"/work/{idx}", provider="codex")
        await store.set_pending_session_pick(idx, True, provider="codex")

    await asyncio.gather(*(_write(idx) for idx in range(1, 11)))
    await store.close()

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == SCHEMA_VERSION
    assert len(payload["users"]) == 10
    assert payload["users"]["10"]["providers"]["codex"]["active_session_id"] == "sid-10"
