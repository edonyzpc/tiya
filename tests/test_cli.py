from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from types import SimpleNamespace

from src import cli
from src.config import load_config
from src.instance_lock import BotInstanceLock
from src.process_utils import ProcessSnapshot
from src.runtime_paths import RuntimePaths, default_working_dir
from src.services.storage import StorageConfig, StorageManager
from src.supervisor_client import SupervisorUnavailableError


def test_parse_dotenv_line_handles_export_quote_and_comment():
    assert cli._parse_dotenv_line('export TELEGRAM_BOT_TOKEN="123:abc"') == ("TELEGRAM_BOT_TOKEN", "123:abc")
    assert cli._parse_dotenv_line("TG_STREAM_ENABLED=1 # inline comment") == ("TG_STREAM_ENABLED", "1")
    assert cli._parse_dotenv_line("  # comment  ") is None


def test_load_dotenv_reads_env_file(monkeypatch, tmp_path: Path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                'TELEGRAM_BOT_TOKEN="123456:abcdefghijklmnopqrstuvwxyz12345"',
                "ALLOWED_TELEGRAM_USER_IDS=123,456",
                "TG_STREAM_ENABLED=0",
            ]
        ),
        encoding="utf-8",
    )

    monkeypatch.setenv("ENV_FILE", str(env_file))
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("ALLOWED_TELEGRAM_USER_IDS", raising=False)
    monkeypatch.delenv("TG_STREAM_ENABLED", raising=False)

    try:
        cli.load_dotenv()

        assert cli._env_value(cli.os.environ, "TELEGRAM_BOT_TOKEN")
        assert cli.os.environ["ALLOWED_TELEGRAM_USER_IDS"] == "123,456"
        assert cli.os.environ["TG_STREAM_ENABLED"] == "0"
    finally:
        for key in ("TELEGRAM_BOT_TOKEN", "ALLOWED_TELEGRAM_USER_IDS", "TG_STREAM_ENABLED"):
            cli.os.environ.pop(key, None)


def test_proxy_normalization_respects_priority(monkeypatch):
    monkeypatch.setenv("TG_PROXY_URL", "http://tg-proxy:7897")
    monkeypatch.setenv("HTTPS_PROXY", "http://https-proxy:7897")
    monkeypatch.setenv("HTTP_PROXY", "http://http-proxy:7897")

    cli.normalize_proxy_env(cli.os.environ)

    assert cli.os.environ["HTTPS_PROXY"] == "http://tg-proxy:7897"
    assert cli.os.environ["https_proxy"] == "http://tg-proxy:7897"
    assert cli.os.environ["HTTP_PROXY"] == "http://tg-proxy:7897"
    assert cli.os.environ["http_proxy"] == "http://tg-proxy:7897"


def test_proxy_is_optional(monkeypatch):
    token = "123456:abcdefghijklmnopqrstuvwxyz12345"
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", token)
    for key in ("TG_PROXY_URL", "HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
        monkeypatch.delenv(key, raising=False)

    cli.normalize_proxy_env(cli.os.environ)
    child_env = cli._build_child_env(RuntimePaths.for_token(token))

    assert "TG_PROXY_URL" not in child_env
    assert "HTTPS_PROXY" not in child_env
    assert "https_proxy" not in child_env
    assert "HTTP_PROXY" not in child_env
    assert "http_proxy" not in child_env


def test_is_pid_running_accepts_packaged_worker(monkeypatch):
    monkeypatch.setattr(
        cli,
        "read_process_snapshot",
        lambda pid: ProcessSnapshot(
            pid=pid,
            stat="S",
            cmdline="/opt/tiya/resources/tiya-backend/tiya-worker/tiya-worker",
        ),
    )

    assert cli._is_pid_running(43210) is True


def test_tg_is_running_removes_stale_pid(monkeypatch, tmp_path: Path):
    paths = RuntimePaths.for_instance_name(root=tmp_path, instance_name="instance-a")
    paths.instance_dir.mkdir(parents=True, exist_ok=True)
    paths.pid_file.write_text("99999", encoding="utf-8")

    monkeypatch.setattr(cli, "_is_pid_running", lambda pid: False)
    monkeypatch.setattr(cli, "_read_lock_owner_pid", lambda _paths: None)

    running, pid = cli.tg_is_running(paths)

    assert running is False
    assert pid is None
    assert not paths.pid_file.exists()


def test_start_uses_module_entry_and_writes_pid(monkeypatch, tmp_path: Path):
    token = "123456:abcdefghijklmnopqrstuvwxyz12345"
    paths = RuntimePaths.for_token(token, {"TIYA_HOME": str(tmp_path)})

    monkeypatch.setattr(cli, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(cli, "validate_tg_config", lambda: None)
    monkeypatch.setattr(cli, "validate_shared_config", lambda: None)
    monkeypatch.setattr(cli, "has_tg_config", lambda: True)
    monkeypatch.setattr(cli, "_require_runtime_paths", lambda: paths)
    monkeypatch.setattr(cli, "_build_child_env", lambda runtime_paths: {"A": "B"})
    monkeypatch.setattr(cli, "_probe_instance_lock", lambda env, runtime_paths: (True, ""))
    monkeypatch.setattr(cli, "tg_is_running", lambda runtime_paths: (False, None))
    monkeypatch.setattr(cli, "_wait_for_ready", lambda proc, runtime_paths: True)

    called = {}

    class _FakeProc:
        pid = 43210

        @staticmethod
        def poll():
            return None

    def _fake_popen(cmd, cwd, env, stdin, stdout, stderr, start_new_session):
        called["cmd"] = cmd
        called["cwd"] = cwd
        called["env"] = env
        called["stdout"] = stdout
        called["stderr"] = stderr
        called["start_new_session"] = start_new_session
        return _FakeProc()

    monkeypatch.setattr(cli.subprocess, "Popen", _fake_popen)

    rc = cli.start()

    assert rc == 0
    assert paths.pid_file.read_text(encoding="utf-8").strip() == "43210"
    assert called["cmd"] == [cli.sys.executable, "-m", cli.BOT_MODULE]
    assert called["cwd"] == str(tmp_path)
    assert called["start_new_session"] is True


def test_build_child_env_matches_config_runner_resolution(monkeypatch, tmp_path: Path):
    token = "123456:abcdefghijklmnopqrstuvwxyz12345"
    expected_codex = "/Applications/Codex.app/Contents/Resources/codex"
    expected_claude = "/opt/homebrew/bin/claude"

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", token)
    monkeypatch.setenv("DEFAULT_CWD", str(tmp_path))
    monkeypatch.delenv("CODEX_BIN", raising=False)
    monkeypatch.delenv("CLAUDE_BIN", raising=False)
    monkeypatch.setattr(cli, "resolve_codex_bin", lambda configured: expected_codex)
    monkeypatch.setattr(cli, "resolve_claude_bin", lambda configured: expected_claude)
    monkeypatch.setattr("src.config.resolve_codex_bin_default", lambda configured: expected_codex)
    monkeypatch.setattr("src.config.resolve_claude_bin_default", lambda configured: expected_claude)

    runtime_paths = RuntimePaths.for_token(token, {"TIYA_HOME": str(tmp_path)})
    child_env = cli._build_child_env(runtime_paths)
    config = load_config()

    assert child_env["CODEX_BIN"] == expected_codex
    assert child_env["CLAUDE_BIN"] == expected_claude
    assert child_env["CODEX_BIN"] == config.codex_bin
    assert child_env["CLAUDE_BIN"] == config.claude_bin
    assert child_env["CODEX_SESSION_ROOT"] == str(config.codex_session_root)
    assert child_env["CLAUDE_SESSION_ROOT"] == str(config.claude_session_root)


def test_build_child_env_defaults_default_cwd_to_home_hidden_dir(monkeypatch, tmp_path: Path):
    token = "123456:abcdefghijklmnopqrstuvwxyz12345"
    runtime_paths = RuntimePaths.for_token(token, {"TIYA_HOME": str(tmp_path)})

    monkeypatch.delenv("DEFAULT_CWD", raising=False)

    child_env = cli._build_child_env(runtime_paths)

    assert child_env["DEFAULT_CWD"] == str(default_working_dir())


def test_start_rejects_when_instance_lock_is_occupied(monkeypatch, tmp_path: Path):
    token = "123456:abcdefghijklmnopqrstuvwxyz12345"
    paths = RuntimePaths.for_token(token, {"TIYA_HOME": str(tmp_path)})

    monkeypatch.setattr(cli, "validate_tg_config", lambda: None)
    monkeypatch.setattr(cli, "validate_shared_config", lambda: None)
    monkeypatch.setattr(cli, "has_tg_config", lambda: True)
    monkeypatch.setattr(cli, "_require_runtime_paths", lambda: paths)
    monkeypatch.setattr(
        cli,
        "_build_child_env",
        lambda runtime_paths: {"TELEGRAM_BOT_TOKEN": token},
    )
    monkeypatch.setattr(cli, "_probe_instance_lock", lambda env, runtime_paths: (False, "occupied"))

    called = {}

    def _fake_popen(*args, **kwargs):
        called["popen_called"] = True
        raise AssertionError("Popen should not be called when lock is occupied")

    monkeypatch.setattr(cli.subprocess, "Popen", _fake_popen)

    rc = cli.start()

    assert rc == 1
    assert "popen_called" not in called


def test_start_rejects_unsupported_storage_schema_before_launch(monkeypatch, tmp_path: Path, capsys):
    token = "123456:abcdefghijklmnopqrstuvwxyz12345"
    paths = RuntimePaths.for_token(token, {"TIYA_HOME": str(tmp_path)})
    paths.storage_dir.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(str(paths.db_file)) as conn:
        conn.execute("PRAGMA user_version=1")

    monkeypatch.setattr(cli, "validate_tg_config", lambda: None)
    monkeypatch.setattr(cli, "validate_shared_config", lambda: None)
    monkeypatch.setattr(cli, "has_tg_config", lambda: True)
    monkeypatch.setattr(cli, "_require_runtime_paths", lambda: paths)
    monkeypatch.setattr(cli, "_build_child_env", lambda runtime_paths: {"TELEGRAM_BOT_TOKEN": token})
    monkeypatch.setattr(cli, "_probe_instance_lock", lambda env, runtime_paths: (True, ""))
    monkeypatch.setattr(cli, "tg_is_running", lambda runtime_paths: (False, None))

    called = {}

    def _fake_popen(*args, **kwargs):
        called["popen_called"] = True
        raise AssertionError("Popen should not be called for unsupported schema")

    monkeypatch.setattr(cli.subprocess, "Popen", _fake_popen)

    rc = cli.start()
    out = capsys.readouterr().out

    assert rc == 1
    assert "storage schema 1 is not supported" in out
    assert "uv run storage rebuild" in out
    assert "popen_called" not in called


def test_wait_for_ready_prefers_worker_state(tmp_path: Path):
    token = "123456:abcdefghijklmnopqrstuvwxyz12345"
    paths = RuntimePaths.for_token(token, {"TIYA_HOME": str(tmp_path)})
    paths.instance_dir.mkdir(parents=True, exist_ok=True)
    paths.worker_state_file.write_text('{"phase":"running","readyAt":123}', encoding="utf-8")

    class _Proc:
        @staticmethod
        def poll():
            return None

    assert cli._wait_for_ready(_Proc(), paths, timeout_sec=0.2) is True


def test_is_pid_running_rejects_zombie_snapshot(monkeypatch):
    monkeypatch.setattr(
        "src.cli.read_process_snapshot",
        lambda pid: ProcessSnapshot(pid=pid, stat="Z", cmdline="python -m src"),
    )

    assert cli._is_pid_running(321) is False


def test_is_pid_running_rejects_defunct_snapshot(monkeypatch):
    monkeypatch.setattr(
        "src.cli.read_process_snapshot",
        lambda pid: ProcessSnapshot(pid=pid, stat="S", cmdline="python <defunct>"),
    )

    assert cli._is_pid_running(654) is False


def test_probe_instance_lock_allows_stale_lock_file(tmp_path: Path):
    token = "123456:abcdefghijklmnopqrstuvwxyz12345"
    paths = RuntimePaths.for_instance_name(root=tmp_path, instance_name="instance-a")
    lock = BotInstanceLock(paths.lock_base, token)
    lock.path.parent.mkdir(parents=True, exist_ok=True)
    lock.path.write_text('{"pid":999999,"started_at":"old"}', encoding="utf-8")

    env = {
        "TELEGRAM_BOT_TOKEN": token,
    }

    ok, msg = cli._probe_instance_lock(env, paths)

    assert ok is True
    assert msg == ""


def test_probe_instance_lock_rejects_when_held(monkeypatch, tmp_path: Path):
    token = "123456:abcdefghijklmnopqrstuvwxyz12345"
    paths = RuntimePaths.for_instance_name(root=tmp_path, instance_name="instance-a")
    monkeypatch.setattr("src.instance_lock.read_process_cmdline", lambda pid: "python -m src")
    holder = BotInstanceLock(paths.lock_base, token)
    acquired, _ = holder.acquire()
    assert acquired is True

    try:
        env = {
            "TELEGRAM_BOT_TOKEN": token,
        }
        ok, msg = cli._probe_instance_lock(env, paths)
        assert ok is False
        assert "owner_pid=" in msg
        assert "python -m src" in msg
    finally:
        holder.release()


def test_storage_cli_stats_does_not_create_cli_instance(monkeypatch, tmp_path: Path, capsys):
    db_path = tmp_path / "storage" / "tiya.db"

    async def _seed() -> None:
        manager = await StorageManager.open(
            StorageConfig(
                db_path=db_path,
                instance_id="service-instance",
                attachments_root=tmp_path / "attachments",
            )
        )
        await manager.maintenance.stats()
        await manager.close()

    asyncio.run(_seed())

    monkeypatch.setattr(cli, "_resolve_storage_db_path", lambda: db_path)
    monkeypatch.setattr(cli.sys, "argv", ["storage", "stats"])

    assert cli.storage() == 0
    out = capsys.readouterr().out
    assert str(db_path) in out

    with sqlite3.connect(str(db_path)) as conn:
        rows = conn.execute("SELECT instance_id FROM instances ORDER BY instance_id").fetchall()
    assert rows == [("service-instance",)]


def test_storage_cli_rebuild_uses_config_and_prints_backup(monkeypatch, tmp_path: Path, capsys):
    token = "123456:abcdefghijklmnopqrstuvwxyz12345"
    monkeypatch.setenv("TIYA_HOME", str(tmp_path))
    runtime_paths = RuntimePaths.for_token(token, {"TIYA_HOME": str(tmp_path)})
    config = SimpleNamespace(
        telegram_token=token,
        storage_path=runtime_paths.db_file,
        default_provider="codex",
        codex_session_root=tmp_path / "codex-sessions",
        claude_session_root=tmp_path / "claude-sessions",
    )
    config.codex_session_root.mkdir(parents=True, exist_ok=True)
    config.claude_session_root.mkdir(parents=True, exist_ok=True)

    called = {}

    async def _fake_rebuild_database(**kwargs):
        called.update(kwargs)
        return runtime_paths.db_file, runtime_paths.db_file.with_name("tiya.db.bak-test")

    monkeypatch.setattr(cli, "load_config", lambda: config)
    monkeypatch.setattr(cli, "list_runtime_instances", lambda environ=None: [])
    monkeypatch.setattr(cli.StorageManager, "rebuild_database", _fake_rebuild_database)
    monkeypatch.setattr(cli.sys, "argv", ["storage", "rebuild"])

    assert cli.storage() == 0
    out = capsys.readouterr().out

    assert str(runtime_paths.db_file) in out
    assert "旧库备份" in out
    assert called["db_path"] == runtime_paths.db_file
    assert called["instance_id"] == runtime_paths.instance_name
    assert called["attachments_root"] == runtime_paths.attachments_dir


def test_top_level_diagnostics_report_attaches_to_supervisor(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_bootstrap", lambda verbose=True: None)
    monkeypatch.setattr(cli, "_rpc_call", lambda method, params=None: {"method": method, "params": params or {}})

    rc = cli.main(["diagnostics", "report"])
    out = capsys.readouterr().out.strip()

    assert rc == 0
    assert '"method": "diagnostics.report"' in out


def test_top_level_diagnostics_export_forwards_destination(monkeypatch, capsys, tmp_path: Path):
    monkeypatch.setattr(cli, "_bootstrap", lambda verbose=True: None)
    monkeypatch.setattr(cli, "_rpc_call", lambda method, params=None: {"method": method, "params": params or {}})

    destination = tmp_path / "doctor.zip"
    rc = cli.main(["diagnostics", "export", str(destination)])
    out = capsys.readouterr().out.strip()

    assert rc == 0
    assert '"method": "diagnostics.export"' in out
    assert str(destination) in out


def test_public_status_reports_supervisor_unavailable_without_booting(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_bootstrap", lambda verbose=True: None)
    monkeypatch.setattr(
        cli,
        "call_rpc",
        lambda method, params=None, environ=None: (_ for _ in ()).throw(
            SupervisorUnavailableError("desktop-owned supervisor is unavailable: /tmp/tiya.sock")
        ),
    )

    rc = cli.main(["status"])
    out = capsys.readouterr().out

    assert rc == 1
    assert "desktop-owned supervisor is unavailable" in out


def test_ctl_service_status_returns_structured_supervisor_unavailable(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_bootstrap", lambda verbose=True: None)
    monkeypatch.setattr(
        cli,
        "call_rpc",
        lambda method, params=None, environ=None: (_ for _ in ()).throw(
            SupervisorUnavailableError("desktop-owned supervisor is unavailable: /tmp/tiya.sock")
        ),
    )

    rc = cli.main(["ctl", "service", "status"])
    out = capsys.readouterr().out

    assert rc == 1
    assert '"code": "supervisor_unavailable"' in out
    assert "desktop-owned supervisor is unavailable" in out
