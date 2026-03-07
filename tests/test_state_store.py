import asyncio
import json
from pathlib import Path

import pytest

from src.domain.models import PendingImage
from src.services.state_store import StateStore


@pytest.mark.asyncio
async def test_state_store_provider_roundtrip(tmp_path: Path):
    path = tmp_path / "state.json"
    store = StateStore(path, default_provider="codex", flush_delay_sec=0.01)

    await store.set_active_provider(1001, "claude")
    await store.set_active_session(1001, "sid-c", "/work-c", provider="claude")
    await store.set_active_session(1001, "sid-x", "/work-x", provider="codex")
    await store.set_last_session_ids(1001, ["sid-c", "sid-c2"], provider="claude")
    await store.set_pending_session_pick(1001, True, provider="claude")
    await store.set_pending_image(
        1001,
        PendingImage(
            path=tmp_path / "pending.png",
            file_name="pending.png",
            mime_type="image/png",
            file_size=123,
            message_id=42,
            created_at=1700000000,
        ),
        provider="claude",
    )
    await store.close()

    store2 = StateStore(path, default_provider="codex")
    assert await store2.get_active_provider(1001) == "claude"
    assert await store2.get_active(1001, provider="claude") == ("sid-c", "/work-c")
    assert await store2.get_active(1001, provider="codex") == ("sid-x", "/work-x")
    assert await store2.get_last_session_ids(1001, provider="claude") == ["sid-c", "sid-c2"]
    assert await store2.is_pending_session_pick(1001, provider="claude") is True
    pending = await store2.get_pending_image(1001, provider="claude")
    assert pending is not None
    assert pending.file_name == "pending.png"
    assert pending.message_id == 42
    await store2.close()


@pytest.mark.asyncio
async def test_legacy_schema_migrates_into_sqlite(tmp_path: Path):
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
    assert store.path.exists()
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
async def test_pending_image_is_provider_scoped_and_clear_returns_previous(tmp_path: Path):
    path = tmp_path / "state.json"
    store = StateStore(path, default_provider="codex", flush_delay_sec=0.01)
    codex_image = PendingImage(
        path=tmp_path / "codex.png",
        file_name="codex.png",
        mime_type="image/png",
        file_size=100,
        message_id=11,
        created_at=1700000001,
    )
    claude_image = PendingImage(
        path=tmp_path / "claude.png",
        file_name="claude.png",
        mime_type="image/png",
        file_size=101,
        message_id=12,
        created_at=1700000002,
    )
    await store.set_pending_image(1, codex_image, provider="codex")
    await store.set_pending_image(1, claude_image, provider="claude")

    assert (await store.get_pending_image(1, provider="codex")) == codex_image
    assert (await store.get_pending_image(1, provider="claude")) == claude_image

    cleared = await store.clear_pending_image(1, provider="claude")

    assert cleared == claude_image
    assert await store.get_pending_image(1, provider="claude") is None
    assert await store.get_pending_image(1, provider="codex") == codex_image
    await store.close()


@pytest.mark.asyncio
async def test_clear_pending_image_keeps_state_when_materialize_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    path = tmp_path / "state.json"
    store = StateStore(path, default_provider="codex", flush_delay_sec=0.01)
    image_path = tmp_path / "pending.png"
    image_path.write_bytes(b"pending-image-bytes")
    pending_image = PendingImage(
        path=image_path,
        file_name="pending.png",
        mime_type="image/png",
        file_size=image_path.stat().st_size,
        message_id=21,
        created_at=1700000003,
    )
    await store.set_pending_image(1, pending_image, provider="codex")

    def _boom(*args: object, **kwargs: object) -> Path:
        raise OSError("disk full")

    monkeypatch.setattr(store.storage, "_materialize_attachment_ref_sync", _boom)
    with pytest.raises(OSError, match="disk full"):
        await store.clear_pending_image(1, provider="codex")

    monkeypatch.undo()
    recovered = await store.get_pending_image(1, provider="codex")
    assert recovered is not None
    assert recovered.file_name == "pending.png"
    assert recovered.message_id == 21
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

    store2 = StateStore(path, default_provider="codex")
    assert await store2.get_active(10, provider="codex") == ("sid-10", "/work/10")
    assert await store2.is_pending_session_pick(10, provider="codex") is True
    await store2.close()
