from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from src import managed_config
from src import cli as legacy_cli
from src.runtime_paths import resolve_supervisor_paths
from src.supervisor import TiyaSupervisor
from src.envfile import read_env_file, write_env_file
from src.secret_store import TELEGRAM_TOKEN_SECRET
import src.secret_store as secret_store_module


def test_supervisor_migrates_legacy_env_secret(monkeypatch, tmp_path: Path):
    env_file = tmp_path / "config" / "tiya.env"
    monkeypatch.setenv("ENV_FILE", str(env_file))
    monkeypatch.setenv("TIYA_HOME", str(tmp_path / "runtime"))
    monkeypatch.setenv("TIYA_SECRET_STORE_BACKEND", "file")

    write_env_file(
        env_file,
        {
            "TELEGRAM_BOT_TOKEN": "123456:abcdefghijklmnopqrstuvwxyz12345",
            "DEFAULT_PROVIDER": "codex",
        },
    )

    supervisor = TiyaSupervisor()
    supervisor._migrate_legacy_secret()

    assert supervisor.secret_store.get(TELEGRAM_TOKEN_SECRET) == "123456:abcdefghijklmnopqrstuvwxyz12345"
    env_values = read_env_file(env_file)
    assert "TELEGRAM_BOT_TOKEN" not in env_values
    assert env_values["DEFAULT_PROVIDER"] == "codex"


def test_resolve_supervisor_paths_migrates_legacy_daemon_runtime_layout(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("TIYA_HOME", str(tmp_path / "runtime"))
    legacy_dir = tmp_path / "runtime" / "daemon"
    legacy_dir.mkdir(parents=True, exist_ok=True)
    (legacy_dir / "tiya.sock").write_text("socket", encoding="utf-8")
    (legacy_dir / "daemon.pid").write_text("123", encoding="utf-8")
    (legacy_dir / "daemon.lock").write_text("lock", encoding="utf-8")
    (legacy_dir / "daemon_state.json").write_text("{}", encoding="utf-8")
    (legacy_dir / "daemon.log").write_text("log", encoding="utf-8")

    paths = resolve_supervisor_paths()

    assert paths.supervisor_dir.name == "supervisor"
    assert paths.socket_file.read_text(encoding="utf-8") == "socket"
    assert paths.pid_file.read_text(encoding="utf-8") == "123"
    assert paths.lock_file.read_text(encoding="utf-8") == "lock"
    assert paths.state_file.read_text(encoding="utf-8") == "{}"
    assert paths.log_file.read_text(encoding="utf-8") == "log"
    assert not legacy_dir.exists()


@pytest.mark.asyncio
async def test_service_status_blocks_when_linux_secret_backend_is_unavailable(monkeypatch, tmp_path: Path):
    env_file = tmp_path / "config" / "tiya.env"
    monkeypatch.setenv("ENV_FILE", str(env_file))
    monkeypatch.setenv("TIYA_HOME", str(tmp_path / "runtime"))
    monkeypatch.delenv("TIYA_SECRET_STORE_BACKEND", raising=False)
    monkeypatch.setattr(secret_store_module.platform, "system", lambda: "Linux")
    original_which = secret_store_module.shutil.which
    monkeypatch.setattr(
        secret_store_module.shutil,
        "which",
        lambda name: None if name == "secret-tool" else original_which(name),
    )
    monkeypatch.setattr(managed_config, "_is_runner_available", lambda _name: True)

    write_env_file(
        env_file,
        {
            "DEFAULT_PROVIDER": "codex",
            "DEFAULT_CWD": str(tmp_path),
        },
    )

    supervisor = TiyaSupervisor()
    status = await supervisor.service_status()

    assert status["phase"] == "unconfigured"
    assert status["blockingIssues"][0]["code"] == "secret_backend_unavailable"
    assert "secret-tool" in status["blockingIssues"][0]["message"]


@pytest.mark.asyncio
async def test_diagnostics_report_and_export_redact_runtime_paths(monkeypatch, tmp_path: Path):
    env_file = tmp_path / "config" / "tiya.env"
    runtime_root = tmp_path / "runtime-home"
    codex_root = tmp_path / "sessions" / "codex"
    claude_root = tmp_path / "sessions" / "claude"
    archive_path = tmp_path / "diagnostics.zip"

    codex_root.mkdir(parents=True)
    claude_root.mkdir(parents=True)

    monkeypatch.setenv("ENV_FILE", str(env_file))
    monkeypatch.setenv("TIYA_HOME", str(runtime_root))
    monkeypatch.setenv("TIYA_SECRET_STORE_BACKEND", "file")
    monkeypatch.setattr(managed_config, "_is_runner_available", lambda _name: True)

    write_env_file(
        env_file,
        {
            "DEFAULT_PROVIDER": "codex",
            "DEFAULT_CWD": str(tmp_path),
            "CODEX_SESSION_ROOT": str(codex_root),
            "CLAUDE_SESSION_ROOT": str(claude_root),
        },
    )

    supervisor = TiyaSupervisor()
    supervisor.secret_store.set(TELEGRAM_TOKEN_SECRET, "123456:abcdefghijklmnopqrstuvwxyz12345")
    token = supervisor._load_token(refresh=True)
    runtime_paths = supervisor._runtime_paths(token)
    assert runtime_paths is not None

    runtime_paths.instance_dir.mkdir(parents=True, exist_ok=True)
    runtime_paths.log_file.write_text(
        "\n".join(
            [
                f"log={runtime_paths.log_file}",
                f"env={env_file}",
                f"session={codex_root}",
            ]
        ),
        encoding="utf-8",
    )

    report = await supervisor.diagnostics_report()
    report_json = json.dumps(report, ensure_ascii=False)

    assert report["envPath"].startswith("[REDACTED_ENV_PATH]")
    assert report["runtimeRoot"] == "[REDACTED_RUNTIME_ROOT]"
    assert report["socketPath"].startswith("[REDACTED_SOCKET_PATH]")
    assert report["storagePath"].startswith("[REDACTED_STORAGE_PATH]")
    assert report["logPath"].startswith("[REDACTED_LOG_PATH]")
    assert all(item["path"] == "[REDACTED_SESSION_ROOT]" for item in report["sessionRoots"])

    for raw_path in (
        str(env_file),
        str(runtime_root),
        str(codex_root),
        str(claude_root),
        str(runtime_paths.log_file),
        str(runtime_paths.db_file),
        str(runtime_paths.pid_file),
    ):
        assert raw_path not in report_json

    await supervisor.diagnostics_export({"destinationPath": str(archive_path)})
    with zipfile.ZipFile(archive_path) as archive:
        doctor_json = archive.read("doctor.json").decode("utf-8")
        logs_text = archive.read("logs.txt").decode("utf-8")

    for raw_path in (
        str(env_file),
        str(runtime_root),
        str(codex_root),
        str(claude_root),
        str(runtime_paths.log_file),
        str(runtime_paths.db_file),
    ):
        assert raw_path not in doctor_json
        assert raw_path not in logs_text


@pytest.mark.asyncio
async def test_service_status_omits_lock_blocker_for_running_worker(monkeypatch, tmp_path: Path):
    env_file = tmp_path / "config" / "tiya.env"
    monkeypatch.setenv("ENV_FILE", str(env_file))
    monkeypatch.setenv("TIYA_HOME", str(tmp_path / "runtime"))
    monkeypatch.setenv("TIYA_SECRET_STORE_BACKEND", "file")
    monkeypatch.setattr(managed_config, "_is_runner_available", lambda _name: True)

    write_env_file(
        env_file,
        {
            "DEFAULT_PROVIDER": "claude",
            "DEFAULT_CWD": str(tmp_path),
        },
    )

    supervisor = TiyaSupervisor()
    supervisor.secret_store.set(TELEGRAM_TOKEN_SECRET, "123456:abcdefghijklmnopqrstuvwxyz12345")
    token = supervisor._load_token(refresh=True)
    runtime_paths = supervisor._runtime_paths(token)
    assert runtime_paths is not None
    runtime_paths.instance_dir.mkdir(parents=True, exist_ok=True)
    runtime_paths.worker_state_file.write_text(
        json.dumps({"phase": "running", "pid": 43210, "readyAt": 1234567890}),
        encoding="utf-8",
    )

    monkeypatch.setattr(supervisor, "_worker_process_status", lambda _paths, _token: (True, 43210))
    monkeypatch.setattr(
        supervisor,
        "_lock_status",
        lambda _token, _paths, _env_values: {"conflict": True, "message": "expected self-owned lock"},
    )

    status = await supervisor.service_status()

    assert status["phase"] == "running"
    assert status["blockingIssues"] == []


@pytest.mark.asyncio
async def test_start_service_records_launch_id(monkeypatch, tmp_path: Path):
    env_file = tmp_path / "config" / "tiya.env"
    monkeypatch.setenv("ENV_FILE", str(env_file))
    monkeypatch.setenv("TIYA_HOME", str(tmp_path / "runtime"))
    monkeypatch.setenv("TIYA_SECRET_STORE_BACKEND", "file")
    monkeypatch.setattr(managed_config, "_is_runner_available", lambda _name: True)

    write_env_file(
        env_file,
        {
            "DEFAULT_PROVIDER": "claude",
            "DEFAULT_CWD": str(tmp_path),
        },
    )

    supervisor = TiyaSupervisor()
    supervisor.secret_store.set(TELEGRAM_TOKEN_SECRET, "123456:abcdefghijklmnopqrstuvwxyz12345")
    token = supervisor._load_token(refresh=True)
    runtime_paths = supervisor._runtime_paths(token)
    assert runtime_paths is not None

    class _FakeProc:
        pid = 54321

        @staticmethod
        def poll():
            return None

    def _fake_spawn_worker(*, env_values, token, launch_id, worker_started_at):
        runtime_paths.instance_dir.mkdir(parents=True, exist_ok=True)
        legacy_cli._write_pid_file(runtime_paths, _FakeProc.pid)
        supervisor._worker_proc = _FakeProc()
        supervisor._write_supervisor_state(
            phase="starting",
            supervisorPid=999,
            workerPid=_FakeProc.pid,
            launchId=launch_id,
            workerStartedAt=worker_started_at,
        )
        return _FakeProc(), runtime_paths

    async def _noop_emit_status() -> None:
        return None

    monkeypatch.setattr(supervisor, "_spawn_worker", _fake_spawn_worker)
    monkeypatch.setattr(supervisor, "_emit_status", _noop_emit_status)

    result = await supervisor.start_service()
    status = result["status"]
    state = supervisor._read_supervisor_state()

    assert result["started"] is True
    assert isinstance(status["launchId"], str)
    assert len(status["launchId"]) == 32
    assert isinstance(status["workerStartedAt"], int)
    assert state["launchId"] == status["launchId"]
    assert state["workerStartedAt"] == status["workerStartedAt"]
