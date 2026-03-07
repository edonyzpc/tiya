from __future__ import annotations

import asyncio
import os
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Awaitable, Callable, Optional, Sequence, TypeVar

import aiosqlite

T = TypeVar("T")

_WAKEUP_TIMEOUT_SEC = 0.01
_DEFAULT_HARD_TIMEOUT_SEC = 30.0
_CONNECT_TIMEOUT_SEC = 10.0
_CLOSE_TIMEOUT_SEC = 5.0


class StorageOperationTimeout(RuntimeError):
    def __init__(self, op_name: str, timeout_sec: float):
        super().__init__(f"storage operation timed out: {op_name} after {timeout_sec:.2f}s")
        self.op_name = op_name
        self.timeout_sec = timeout_sec


def sqlite_managed_paths(db_path: Path) -> tuple[Path, Path, Path]:
    return (
        db_path,
        db_path.with_name(db_path.name + "-wal"),
        db_path.with_name(db_path.name + "-shm"),
    )


def delete_sqlite_files(db_path: Path) -> None:
    for candidate in sqlite_managed_paths(db_path):
        try:
            candidate.unlink(missing_ok=True)
        except OSError:
            pass


async def _drain_task(task: asyncio.Future[Any], *, timeout_sec: float) -> None:
    if task.done():
        try:
            task.result()
        except BaseException:
            pass
        return
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_sec
    task.cancel()
    while True:
        if task.done():
            try:
                task.result()
            except BaseException:
                pass
            return
        remaining = deadline - loop.time()
        if remaining <= 0:
            return
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=min(_WAKEUP_TIMEOUT_SEC, remaining))
        except asyncio.TimeoutError:
            continue
        except BaseException:
            return


async def await_db(
    op_name: str,
    awaitable: Awaitable[T],
    *,
    hard_timeout_sec: float = _DEFAULT_HARD_TIMEOUT_SEC,
    tracked_tasks: Optional[set[asyncio.Future[Any]]] = None,
) -> T:
    task = asyncio.ensure_future(awaitable)
    if tracked_tasks is not None:
        tracked_tasks.add(task)
        task.add_done_callback(tracked_tasks.discard)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + hard_timeout_sec
    try:
        while not task.done():
            remaining = deadline - loop.time()
            if remaining <= 0:
                await _drain_task(task, timeout_sec=_WAKEUP_TIMEOUT_SEC)
                if task.done():
                    try:
                        return task.result()
                    except BaseException:
                        pass
                raise StorageOperationTimeout(op_name, hard_timeout_sec)
            await asyncio.sleep(min(_WAKEUP_TIMEOUT_SEC, remaining))
        return task.result()
    except asyncio.CancelledError:
        await _drain_task(task, timeout_sec=hard_timeout_sec)
        raise
    finally:
        if tracked_tasks is not None and task.done():
            tracked_tasks.discard(task)


class StorageSession:
    def __init__(self, runtime: "StorageRuntime", conn: aiosqlite.Connection):
        self._runtime = runtime
        self._conn = conn
        self._tx_depth = 0
        self._savepoint_seq = 0

    async def execute(self, sql: str, params: Sequence[Any] = (), *, op_name: str = "execute") -> None:
        cursor = await self._runtime._await_db(f"{op_name}:execute", self._conn.execute(sql, tuple(params)))
        await self._runtime._await_db(f"{op_name}:cursor_close", cursor.close())

    async def execute_insert(self, sql: str, params: Sequence[Any] = (), *, op_name: str = "insert") -> int:
        cursor = await self._runtime._await_db(f"{op_name}:execute", self._conn.execute(sql, tuple(params)))
        try:
            return int(cursor.lastrowid or 0)
        finally:
            await self._runtime._await_db(f"{op_name}:cursor_close", cursor.close())

    async def executemany(
        self,
        sql: str,
        rows: Sequence[Sequence[Any]],
        *,
        op_name: str = "executemany",
    ) -> None:
        cursor = await self._runtime._await_db(
            f"{op_name}:execute",
            self._conn.executemany(sql, [tuple(row) for row in rows]),
        )
        await self._runtime._await_db(f"{op_name}:cursor_close", cursor.close())

    async def fetch_one(
        self,
        sql: str,
        params: Sequence[Any] = (),
        *,
        op_name: str = "fetch_one",
    ) -> Optional[sqlite3.Row]:
        cursor = await self._runtime._await_db(f"{op_name}:execute", self._conn.execute(sql, tuple(params)))
        try:
            return await self._runtime._await_db(f"{op_name}:fetchone", cursor.fetchone())
        finally:
            await self._runtime._await_db(f"{op_name}:cursor_close", cursor.close())

    async def fetch_all(
        self,
        sql: str,
        params: Sequence[Any] = (),
        *,
        op_name: str = "fetch_all",
    ) -> list[sqlite3.Row]:
        cursor = await self._runtime._await_db(f"{op_name}:execute", self._conn.execute(sql, tuple(params)))
        try:
            rows = await self._runtime._await_db(f"{op_name}:fetchall", cursor.fetchall())
            return list(rows)
        finally:
            await self._runtime._await_db(f"{op_name}:cursor_close", cursor.close())

    async def fetch_value(
        self,
        sql: str,
        params: Sequence[Any] = (),
        *,
        op_name: str = "fetch_value",
        default: Any = None,
    ) -> Any:
        row = await self.fetch_one(sql, params, op_name=op_name)
        if row is None:
            return default
        return row[0]

    @asynccontextmanager
    async def transaction(self, mode: str = "IMMEDIATE") -> AsyncIterator["StorageSession"]:
        savepoint: Optional[str] = None
        if self._tx_depth == 0:
            await self.execute(f"BEGIN {mode}", op_name="tx_begin")
        else:
            savepoint = f"tiya_sp_{self._savepoint_seq}"
            self._savepoint_seq += 1
            await self.execute(f"SAVEPOINT {savepoint}", op_name="tx_savepoint")
        self._tx_depth += 1
        try:
            yield self
        except Exception:
            if savepoint is None:
                await self.execute("ROLLBACK", op_name="tx_rollback")
            else:
                await self.execute(f"ROLLBACK TO SAVEPOINT {savepoint}", op_name="tx_rollback_to")
                await self.execute(f"RELEASE SAVEPOINT {savepoint}", op_name="tx_release_after_rollback")
            raise
        else:
            if savepoint is None:
                await self.execute("COMMIT", op_name="tx_commit")
            else:
                await self.execute(f"RELEASE SAVEPOINT {savepoint}", op_name="tx_release")
        finally:
            self._tx_depth -= 1


class StorageRuntime:
    def __init__(self, db_path: Path):
        self.db_path = db_path.expanduser()
        self._call_lock = asyncio.Lock()
        self._conn: Optional[aiosqlite.Connection] = None
        self._startup_error: Optional[BaseException] = None
        self._closing = False
        self._closed = False
        self._inflight_tasks: set[asyncio.Future[Any]] = set()

    @classmethod
    async def open(cls, db_path: Path) -> "StorageRuntime":
        runtime = cls(db_path)
        try:
            await runtime._open()
        except BaseException as exc:  # noqa: BLE001
            if isinstance(exc, RuntimeError):
                raise
            raise RuntimeError("failed to initialize sqlite storage") from exc
        return runtime

    async def _open(self) -> None:
        async with self._call_lock:
            if self._conn is not None:
                return
            if self._startup_error is not None:
                raise RuntimeError("failed to initialize sqlite storage") from self._startup_error
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            conn: Optional[aiosqlite.Connection] = None
            try:
                conn = await self._await_db(
                    "connect",
                    aiosqlite.connect(str(self.db_path), timeout=5.0, isolation_level=None),
                    hard_timeout_sec=_CONNECT_TIMEOUT_SEC,
                )
                conn.row_factory = sqlite3.Row
                session = StorageSession(self, conn)
                await session.execute("PRAGMA journal_mode=WAL", op_name="pragma_journal_mode")
                await session.execute("PRAGMA foreign_keys=ON", op_name="pragma_foreign_keys")
                await session.execute("PRAGMA busy_timeout=5000", op_name="pragma_busy_timeout")
                await session.execute("PRAGMA synchronous=NORMAL", op_name="pragma_synchronous")
            except BaseException as exc:  # noqa: BLE001
                self._startup_error = exc
                if conn is not None:
                    await self._await_db("close_after_open_failure", conn.close(), hard_timeout_sec=_CLOSE_TIMEOUT_SEC)
                raise RuntimeError("failed to initialize sqlite storage") from exc
            self._conn = conn
            self._ensure_permissions()

    async def read(self, fn: Callable[[StorageSession], Awaitable[T]]) -> T:
        return await self._run_callback(fn)

    async def write(self, fn: Callable[[StorageSession], Awaitable[T]], *, mode: str = "IMMEDIATE") -> T:
        async def _write(session: StorageSession) -> T:
            async with session.transaction(mode):
                return await fn(session)

        return await self._run_callback(_write)

    async def close(self) -> None:
        if self._closed:
            return
        self._closing = True
        async with self._call_lock:
            if self._closed:
                return
            await self._drain_inflight_tasks("close_inflight_drain", timeout_sec=_CLOSE_TIMEOUT_SEC)
            conn = self._conn
            self._conn = None
            if conn is not None:
                await self._await_db("close", conn.close(), hard_timeout_sec=_CLOSE_TIMEOUT_SEC)
            self._closed = True

    async def backup(self, destination: Path) -> None:
        if self._closing:
            raise RuntimeError("sqlite runtime is closing")
        async with self._call_lock:
            if self._closing:
                raise RuntimeError("sqlite runtime is closing")
            conn = await self._ensure_open_connection()
            destination.parent.mkdir(parents=True, exist_ok=True)
            backup_conn = await await_db(
                "backup_connect",
                asyncio.to_thread(sqlite3.connect, str(destination), check_same_thread=False),
                hard_timeout_sec=_CONNECT_TIMEOUT_SEC,
            )
            try:
                await self._await_db("backup", conn.backup(backup_conn))
            finally:
                await await_db(
                    "backup_close_destination",
                    asyncio.to_thread(backup_conn.close),
                    hard_timeout_sec=_CLOSE_TIMEOUT_SEC,
                )
            self._ensure_permissions()
            try:
                os.chmod(destination, 0o600)
            except OSError:
                pass

    async def checkpoint_truncate(self) -> None:
        await self.read(lambda session: session.execute("PRAGMA wal_checkpoint(TRUNCATE)", op_name="checkpoint_truncate"))

    async def vacuum(self) -> None:
        async def _vacuum(session: StorageSession) -> None:
            await session.execute("PRAGMA wal_checkpoint(TRUNCATE)", op_name="vacuum_checkpoint_before")
            await session.execute("VACUUM", op_name="vacuum")
            await session.execute("PRAGMA wal_checkpoint(TRUNCATE)", op_name="vacuum_checkpoint_after")

        await self.read(_vacuum)

    async def _run_callback(self, fn: Callable[[StorageSession], Awaitable[T]]) -> T:
        if self._closing:
            raise RuntimeError("sqlite runtime is closing")
        async with self._call_lock:
            if self._closing:
                raise RuntimeError("sqlite runtime is closing")
            conn = await self._ensure_open_connection()
            session = StorageSession(self, conn)
            try:
                return await fn(session)
            finally:
                self._ensure_permissions()

    async def _ensure_open_connection(self) -> aiosqlite.Connection:
        if self._closed:
            raise RuntimeError("sqlite runtime is closed")
        if self._startup_error is not None:
            raise RuntimeError("failed to initialize sqlite storage") from self._startup_error
        if self._conn is None:
            raise RuntimeError("sqlite runtime is not open")
        return self._conn

    async def _await_db(
        self,
        op_name: str,
        awaitable: Awaitable[T],
        *,
        hard_timeout_sec: float = _DEFAULT_HARD_TIMEOUT_SEC,
    ) -> T:
        return await await_db(
            op_name,
            awaitable,
            hard_timeout_sec=hard_timeout_sec,
            tracked_tasks=self._inflight_tasks,
        )

    async def _drain_inflight_tasks(self, op_name: str, *, timeout_sec: float) -> None:
        if not self._inflight_tasks:
            return
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_sec
        while self._inflight_tasks:
            for task in tuple(self._inflight_tasks):
                if not task.done():
                    continue
                self._inflight_tasks.discard(task)
                try:
                    task.result()
                except BaseException:
                    pass
            if not self._inflight_tasks:
                return
            remaining = deadline - loop.time()
            if remaining <= 0:
                pending = tuple(self._inflight_tasks)
                for task in pending:
                    await _drain_task(task, timeout_sec=_WAKEUP_TIMEOUT_SEC)
                if self._inflight_tasks:
                    raise StorageOperationTimeout(op_name, timeout_sec)
                return
            done, _pending = await asyncio.wait(tuple(self._inflight_tasks), timeout=min(_WAKEUP_TIMEOUT_SEC, remaining))
            for task in done:
                try:
                    task.result()
                except BaseException:
                    pass

    def _ensure_permissions(self) -> None:
        for path in sqlite_managed_paths(self.db_path):
            if path.exists():
                try:
                    os.chmod(path, 0o600)
                except OSError:
                    pass
