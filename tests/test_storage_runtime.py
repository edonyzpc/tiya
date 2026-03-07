import asyncio
import sqlite3
from pathlib import Path

import pytest

from src.domain.models import ActiveRunState
from src.services.storage import StorageConfig, StorageManager, StorageOperationTimeout


def _config(tmp_path: Path, *, session_roots=None) -> StorageConfig:
    return StorageConfig(
        db_path=tmp_path / "storage" / "tiya.db",
        instance_id="runtime-test",
        attachments_root=tmp_path / "attachments",
        session_roots=session_roots or {},
    )


def test_storage_manager_open_stats_close_with_asyncio_run(tmp_path: Path):
    async def _main() -> None:
        manager = await StorageManager.open(_config(tmp_path))
        try:
            stats = await manager.maintenance.stats()
            assert stats["db_path"] == str(tmp_path / "storage" / "tiya.db")
        finally:
            await manager.close()
            await manager.close()

    asyncio.run(asyncio.wait_for(_main(), timeout=5))


@pytest.mark.asyncio
async def test_storage_manager_serializes_concurrent_state_ops_without_hanging(tmp_path: Path):
    manager = await StorageManager.open(_config(tmp_path))
    try:
        async def _write(idx: int) -> None:
            await manager.state.set_active_session(idx, "codex", f"sid-{idx}", f"/work/{idx}")
            await manager.state.set_pending_session_pick(idx, "codex", True)
            await manager.state.set_active_run(
                idx,
                "codex",
                ActiveRunState(
                    run_id=f"run-{idx}",
                    chat_id=100 + idx,
                    chat_type="private",
                    started_at=1700000000 + idx,
                ),
            )
            assert await manager.state.get_active(idx, "codex") == (f"sid-{idx}", f"/work/{idx}")

        async def _read_stats() -> None:
            stats = await manager.maintenance.stats()
            assert "table_counts" in stats

        await asyncio.wait_for(
            asyncio.gather(
                *(_write(idx) for idx in range(1, 11)),
                *(_read_stats() for _ in range(3)),
            ),
            timeout=10,
        )
        assert await manager.state.get_active(10, "codex") == ("sid-10", "/work/10")
        assert await manager.state.is_pending_session_pick(10, "codex") is True
        active_run = await manager.state.get_active_run(10, "codex")
        assert active_run is not None
        assert active_run.run_id == "run-10"
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_storage_close_waits_for_inflight_callback_and_rejects_new_ops(tmp_path: Path):
    manager = await StorageManager.open(_config(tmp_path))
    started = asyncio.Event()
    release = asyncio.Event()
    finished = asyncio.Event()

    async def _hold(db) -> None:
        await db.fetch_value("SELECT 1", op_name="hold_callback_probe")
        started.set()
        await release.wait()
        await db.fetch_value("SELECT 1", op_name="hold_callback_finish")
        finished.set()

    hold_task = asyncio.create_task(manager.runtime.read(_hold))
    await asyncio.wait_for(started.wait(), timeout=2)

    close_task = asyncio.create_task(manager.close())
    await asyncio.sleep(0)

    with pytest.raises(RuntimeError, match="closing"):
        await manager.maintenance.stats()

    assert close_task.done() is False
    release.set()
    await asyncio.wait_for(finished.wait(), timeout=2)
    await asyncio.wait_for(hold_task, timeout=2)
    await asyncio.wait_for(close_task, timeout=2)


@pytest.mark.asyncio
async def test_storage_operation_timeout_clears_runtime_inflight_tasks(tmp_path: Path):
    manager = await StorageManager.open(_config(tmp_path))
    try:
        async def _never() -> int:
            await asyncio.Future()
            return 1

        with pytest.raises(StorageOperationTimeout):
            await manager.runtime._await_db("never_finishes", _never(), hard_timeout_sec=0.05)

        assert manager.runtime._inflight_tasks == set()
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_storage_operation_cancel_drains_runtime_inflight_task(tmp_path: Path):
    manager = await StorageManager.open(_config(tmp_path))
    started = asyncio.Event()
    inner_cancelled = asyncio.Event()

    async def _block() -> int:
        started.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            inner_cancelled.set()
            raise
        return 1

    task = asyncio.create_task(manager.runtime._await_db("cancelled_op", _block(), hard_timeout_sec=1.0))
    await asyncio.wait_for(started.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert inner_cancelled.is_set()
    assert manager.runtime._inflight_tasks == set()
    await manager.close()


@pytest.mark.asyncio
async def test_storage_schema_mismatch_requires_rebuild(tmp_path: Path):
    db_path = tmp_path / "storage" / "tiya.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute("PRAGMA user_version=3")

    with pytest.raises(RuntimeError, match="storage schema 3 is not supported"):
        await StorageManager.open(_config(tmp_path))


@pytest.mark.asyncio
async def test_storage_rebuild_with_missing_roots_creates_empty_db(tmp_path: Path):
    config = _config(
        tmp_path,
        session_roots={
            "codex": tmp_path / "missing-codex",
            "claude": tmp_path / "missing-claude",
        },
    )
    rebuilt_path, backup_path = await StorageManager.rebuild_database(
        db_path=config.db_path,
        instance_id=config.instance_id,
        attachments_root=config.attachments_root,
        session_roots=config.session_roots,
    )
    assert rebuilt_path == config.db_path
    assert backup_path is None

    manager = await StorageManager.open(config)
    try:
        stats = await manager.maintenance.stats()
        assert stats["table_counts"]["sessions"] == 0
        assert stats["table_counts"]["session_raw_lines"] == 0
    finally:
        await manager.close()
