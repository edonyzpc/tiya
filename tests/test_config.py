from pathlib import Path

import pytest

from config import load_config, parse_default_provider, resolve_tg_proxy, resolve_tg_stream_enabled


def test_resolve_tg_stream_enabled_precedence(monkeypatch):
    monkeypatch.setenv("TG_STREAM_ENABLED", "0")
    monkeypatch.setenv("TELEGRAM_ENABLE_DRAFT_STREAM", "1")
    assert resolve_tg_stream_enabled() is False

    monkeypatch.setenv("TG_STREAM_ENABLED", "")
    monkeypatch.setenv("TELEGRAM_ENABLE_DRAFT_STREAM", "0")
    assert resolve_tg_stream_enabled() is False

    monkeypatch.delenv("TG_STREAM_ENABLED", raising=False)
    monkeypatch.delenv("TELEGRAM_ENABLE_DRAFT_STREAM", raising=False)
    assert resolve_tg_stream_enabled() is True


def test_load_config_defaults(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:abcdefghijklmnopqrstuvwxyz12345")
    monkeypatch.setenv("DEFAULT_CWD", str(tmp_path))
    monkeypatch.setenv("STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setenv("CODEX_SESSION_ROOT", str(tmp_path / "sessions"))
    for key in ("TG_PROXY_URL", "HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
        monkeypatch.delenv(key, raising=False)

    config = load_config()

    assert config.telegram_token.startswith("123456:")
    assert config.telegram_proxy is None
    assert config.allowed_user_ids is None
    assert config.stream_enabled is True
    assert config.stream_edit_interval_ms == 700
    assert config.stream_min_delta_chars == 8
    assert config.thinking_status_interval_ms == 900
    assert config.default_cwd == tmp_path
    assert config.state_path == tmp_path / "state.json"
    assert config.default_provider == "codex"
    assert config.codex_session_root == tmp_path / "sessions"
    assert config.claude_session_root == Path("~/.claude/projects").expanduser()
    assert config.claude_bin
    assert config.claude_model is None
    assert config.claude_permission_mode == "default"
    assert config.tg_instance_lock_path == Path("./.runtime/bot.lock")
    assert config.tg_stream_retry_cooldown_ms == 15000
    assert config.tg_stream_max_consecutive_preview_errors == 2
    assert config.tg_stream_preview_failfast is True


def test_resolve_tg_proxy_precedence(monkeypatch):
    monkeypatch.setenv("TG_PROXY_URL", "http://proxy-a:8000")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy-b:8000")
    assert resolve_tg_proxy() == "http://proxy-a:8000"

    monkeypatch.delenv("TG_PROXY_URL", raising=False)
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy-b:8000")
    assert resolve_tg_proxy() == "http://proxy-b:8000"


def test_default_provider_parse():
    assert parse_default_provider(None) == "codex"
    assert parse_default_provider("claude") == "claude"
    assert parse_default_provider(" CoDeX ") == "codex"
    with pytest.raises(ValueError):
        parse_default_provider("gpt")
