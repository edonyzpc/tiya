from __future__ import annotations

from pathlib import Path

import cli
from instance_lock import BotInstanceLock


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
    for key in ("TG_PROXY_URL", "HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
        monkeypatch.delenv(key, raising=False)

    cli.normalize_proxy_env(cli.os.environ)
    child_env = cli._build_child_env()

    assert "TG_PROXY_URL" not in child_env
    assert "HTTPS_PROXY" not in child_env
    assert "https_proxy" not in child_env
    assert "HTTP_PROXY" not in child_env
    assert "http_proxy" not in child_env


def test_tg_is_running_removes_stale_pid(monkeypatch, tmp_path: Path):
    pid_file = tmp_path / "bot.pid"
    pid_file.write_text("99999", encoding="utf-8")

    monkeypatch.setattr(cli, "PID_FILE", pid_file)
    monkeypatch.setattr(cli, "_is_pid_running", lambda pid: False)
    monkeypatch.setattr(cli, "_find_existing_pid", lambda: None)

    running, pid = cli.tg_is_running()

    assert running is False
    assert pid is None
    assert not pid_file.exists()


def test_start_uses_new_entry_and_writes_pid(monkeypatch, tmp_path: Path):
    runtime_dir = tmp_path / ".runtime"
    pid_file = runtime_dir / "bot.pid"
    log_file = runtime_dir / "bot.log"
    state_path = runtime_dir / "bot_state.json"
    entry_file = tmp_path / "tiya.py"
    entry_file.write_text("print('ok')\n", encoding="utf-8")

    monkeypatch.setattr(cli, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(cli, "RUNTIME_DIR", runtime_dir)
    monkeypatch.setattr(cli, "PID_FILE", pid_file)
    monkeypatch.setattr(cli, "LOG_FILE", log_file)
    monkeypatch.setattr(cli, "STATE_PATH", state_path)
    monkeypatch.setattr(cli, "BOT_ENTRY", entry_file)
    monkeypatch.setattr(cli, "tg_is_running", lambda: (False, None))
    monkeypatch.setattr(cli, "validate_tg_config", lambda: None)
    monkeypatch.setattr(cli, "validate_shared_config", lambda: None)
    monkeypatch.setattr(cli, "has_tg_config", lambda: True)
    monkeypatch.setattr(cli, "_build_child_env", lambda: {"A": "B"})

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
        called["start_new_session"] = start_new_session
        return _FakeProc()

    monkeypatch.setattr(cli.subprocess, "Popen", _fake_popen)

    rc = cli.start()

    assert rc == 0
    assert pid_file.read_text(encoding="utf-8").strip() == "43210"
    assert called["cmd"][1] == str(entry_file)
    assert called["cwd"] == str(tmp_path)
    assert called["start_new_session"] is True


def test_start_rejects_when_instance_lock_is_occupied(monkeypatch, tmp_path: Path):
    runtime_dir = tmp_path / ".runtime"
    entry_file = tmp_path / "tiya.py"
    entry_file.write_text("print('ok')\n", encoding="utf-8")

    monkeypatch.setattr(cli, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(cli, "RUNTIME_DIR", runtime_dir)
    monkeypatch.setattr(cli, "PID_FILE", runtime_dir / "bot.pid")
    monkeypatch.setattr(cli, "LOG_FILE", runtime_dir / "bot.log")
    monkeypatch.setattr(cli, "STATE_PATH", runtime_dir / "bot_state.json")
    monkeypatch.setattr(cli, "BOT_ENTRY", entry_file)
    monkeypatch.setattr(cli, "validate_tg_config", lambda: None)
    monkeypatch.setattr(cli, "validate_shared_config", lambda: None)
    monkeypatch.setattr(cli, "has_tg_config", lambda: True)
    monkeypatch.setattr(
        cli,
        "_build_child_env",
        lambda: {"TELEGRAM_BOT_TOKEN": "123456:abcdefghijklmnopqrstuvwxyz12345"},
    )
    monkeypatch.setattr(cli, "_probe_instance_lock", lambda env: (False, "occupied"))

    called = {}

    def _fake_popen(*args, **kwargs):
        called["popen_called"] = True
        raise AssertionError("Popen should not be called when lock is occupied")

    monkeypatch.setattr(cli.subprocess, "Popen", _fake_popen)

    rc = cli.start()

    assert rc == 1
    assert "popen_called" not in called


def test_probe_instance_lock_allows_stale_lock_file(tmp_path: Path):
    lock_path = tmp_path / ".runtime" / "bot.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text('{"pid":999999,"started_at":"old"}', encoding="utf-8")

    env = {
        "TELEGRAM_BOT_TOKEN": "123456:abcdefghijklmnopqrstuvwxyz12345",
        "TG_INSTANCE_LOCK_PATH": str(lock_path),
    }

    ok, msg = cli._probe_instance_lock(env)

    assert ok is True
    assert msg == ""


def test_probe_instance_lock_rejects_when_held(tmp_path: Path):
    lock_base = tmp_path / ".runtime" / "bot.lock"
    token = "123456:abcdefghijklmnopqrstuvwxyz12345"
    holder = BotInstanceLock(lock_base, token)
    acquired, _ = holder.acquire()
    assert acquired is True

    try:
        env = {
            "TELEGRAM_BOT_TOKEN": token,
            "TG_INSTANCE_LOCK_PATH": str(lock_base),
        }
        ok, msg = cli._probe_instance_lock(env)
        assert ok is False
        assert "owner_pid=" in msg
    finally:
        holder.release()
