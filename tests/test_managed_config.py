from __future__ import annotations

from pathlib import Path

from src import managed_config
from src.runtime_paths import RuntimePaths


def test_validate_snapshot_allows_missing_secret(monkeypatch, tmp_path: Path):
    payload = {
        "env": {
            **managed_config.default_env_values(),
            "DEFAULT_CWD": str(tmp_path),
        }
    }

    monkeypatch.setattr(managed_config, "_is_runner_available", lambda _name: True)

    result = managed_config.validate_snapshot(payload, secret_present=False)

    assert result["ok"] is True
    assert "Telegram bot token secret is not configured yet" in result["warnings"]


def test_persist_snapshot_writes_supported_env_values(tmp_path: Path):
    paths = managed_config.ConfigPaths(
        env_file=tmp_path / "tiya.env",
        config_dir=tmp_path,
        secret_metadata_file=tmp_path / ".meta.json",
        secret_file=tmp_path / ".secret.json",
    )
    payload = {
        "env": {
            **managed_config.default_env_values(),
            "DEFAULT_CWD": str(tmp_path),
            "DEFAULT_PROVIDER": "claude",
        }
    }

    persisted = managed_config.persist_snapshot(paths=paths, payload=payload)

    assert persisted["env"]["DEFAULT_PROVIDER"] == "claude"
    content = paths.env_file.read_text(encoding="utf-8")
    assert "DEFAULT_PROVIDER=claude" in content


def test_validate_snapshot_rejects_invalid_desktop_gpu_mode(monkeypatch, tmp_path: Path):
    payload = {
        "env": {
            **managed_config.default_env_values(),
            "DEFAULT_CWD": str(tmp_path),
            "TIYA_DESKTOP_GPU_MODE": "auto",
        }
    }

    monkeypatch.setattr(managed_config, "_is_runner_available", lambda _name: True)

    result = managed_config.validate_snapshot(payload, secret_present=True)

    assert result["ok"] is False
    assert "TIYA_DESKTOP_GPU_MODE must be enabled or disabled" in result["errors"]


def test_validate_snapshot_creates_missing_default_cwd(monkeypatch, tmp_path: Path):
    default_cwd = tmp_path / ".tiya"
    payload = {
        "env": {
            **managed_config.default_env_values(),
            "DEFAULT_CWD": str(default_cwd),
        }
    }

    monkeypatch.setattr(managed_config, "_is_runner_available", lambda _name: True)

    result = managed_config.validate_snapshot(payload, secret_present=True)

    assert result["ok"] is True
    assert default_cwd.is_dir()


def test_build_worker_env_clears_inherited_lowercase_proxy_when_config_is_blank(tmp_path: Path):
    token = "123456:abcdefghijklmnopqrstuvwxyz12345"
    runtime_paths = RuntimePaths.for_token(token, {"TIYA_HOME": str(tmp_path)})
    env_values = managed_config.default_env_values()

    worker_env = managed_config.build_worker_env(
        base_environ={
            "https_proxy": "http://127.0.0.1:7897",
            "http_proxy": "http://127.0.0.1:7897",
            "HTTPS_PROXY": "http://127.0.0.1:7897",
            "HTTP_PROXY": "http://127.0.0.1:7897",
        },
        token=token,
        env_values=env_values,
        runtime_paths=runtime_paths,
    )

    assert worker_env["TG_PROXY_URL"] == ""
    assert worker_env["HTTPS_PROXY"] == ""
    assert worker_env["HTTP_PROXY"] == ""
    assert "https_proxy" not in worker_env
    assert "http_proxy" not in worker_env
