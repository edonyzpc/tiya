from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from .config import (
    parse_allowed_cwd_roots,
    parse_allowed_user_ids,
    parse_bool,
    parse_desktop_gpu_mode,
    parse_dangerous_bypass_level,
    parse_default_provider,
    parse_formatting_backend,
    parse_formatting_mode,
    parse_formatting_style,
    parse_link_preview_policy,
    parse_non_negative_int,
    parse_positive_int,
)
from .envfile import read_env_file, write_env_file
from .provider_defaults import (
    default_claude_session_root,
    default_codex_session_root,
    resolve_claude_bin,
    resolve_codex_bin,
)
from .secret_store import SecretStatus


REPO_ROOT = Path(__file__).resolve().parent.parent
TOKEN_RE = re.compile(r"^[0-9]{6,}:[A-Za-z0-9_-]{20,}$")
USER_IDS_RE = re.compile(r"^[0-9]+(,[0-9]+)*$")

SUPPORTED_ENV_KEYS: tuple[str, ...] = (
    "ALLOWED_TELEGRAM_USER_IDS",
    "ALLOWED_CWD_ROOTS",
    "DEFAULT_CWD",
    "DEFAULT_PROVIDER",
    "TIYA_DESKTOP_GPU_MODE",
    "TG_PROXY_URL",
    "HTTPS_PROXY",
    "HTTP_PROXY",
    "CODEX_BIN",
    "CODEX_SESSION_ROOT",
    "CODEX_SANDBOX_MODE",
    "CODEX_APPROVAL_POLICY",
    "CODEX_DANGEROUS_BYPASS",
    "CLAUDE_BIN",
    "CLAUDE_SESSION_ROOT",
    "CLAUDE_MODEL",
    "CLAUDE_PERMISSION_MODE",
    "TG_STREAM_ENABLED",
    "TG_STREAM_EDIT_INTERVAL_MS",
    "TG_STREAM_MIN_DELTA_CHARS",
    "TG_THINKING_STATUS_INTERVAL_MS",
    "TG_HTTP_MAX_RETRIES",
    "TG_HTTP_RETRY_BASE_MS",
    "TG_HTTP_RETRY_MAX_MS",
    "TG_STREAM_RETRY_COOLDOWN_MS",
    "TG_STREAM_MAX_CONSECUTIVE_PREVIEW_ERRORS",
    "TG_STREAM_PREVIEW_FAILFAST",
    "TG_FORMATTING_ENABLED",
    "TG_FORMATTING_STYLE",
    "TG_FORMATTING_MODE",
    "TG_FORMATTING_BACKEND",
    "TG_LINK_PREVIEW_POLICY",
    "TG_FORMATTING_FAIL_OPEN",
)


@dataclass(frozen=True)
class ConfigPaths:
    env_file: Path
    config_dir: Path
    secret_metadata_file: Path
    secret_file: Path


def resolve_config_paths(environ: Mapping[str, str] | None = None) -> ConfigPaths:
    env = environ or os.environ
    raw = (env.get("ENV_FILE") or "").strip()
    env_file = Path(raw).expanduser() if raw else REPO_ROOT / ".env"
    config_dir = env_file.parent
    return ConfigPaths(
        env_file=env_file,
        config_dir=config_dir,
        secret_metadata_file=config_dir / ".tiya-secret-metadata.json",
        secret_file=config_dir / ".tiya-secrets.json",
    )


def default_env_values() -> dict[str, str]:
    return {
        "ALLOWED_TELEGRAM_USER_IDS": "",
        "ALLOWED_CWD_ROOTS": "",
        "DEFAULT_CWD": str(REPO_ROOT),
        "DEFAULT_PROVIDER": "codex",
        "TIYA_DESKTOP_GPU_MODE": "disabled",
        "TG_PROXY_URL": "",
        "HTTPS_PROXY": "",
        "HTTP_PROXY": "",
        "CODEX_BIN": resolve_codex_bin(None),
        "CODEX_SESSION_ROOT": str(default_codex_session_root()),
        "CODEX_SANDBOX_MODE": "",
        "CODEX_APPROVAL_POLICY": "",
        "CODEX_DANGEROUS_BYPASS": "0",
        "CLAUDE_BIN": resolve_claude_bin(None),
        "CLAUDE_SESSION_ROOT": str(default_claude_session_root()),
        "CLAUDE_MODEL": "",
        "CLAUDE_PERMISSION_MODE": "default",
        "TG_STREAM_ENABLED": "1",
        "TG_STREAM_EDIT_INTERVAL_MS": "700",
        "TG_STREAM_MIN_DELTA_CHARS": "8",
        "TG_THINKING_STATUS_INTERVAL_MS": "900",
        "TG_HTTP_MAX_RETRIES": "2",
        "TG_HTTP_RETRY_BASE_MS": "300",
        "TG_HTTP_RETRY_MAX_MS": "3000",
        "TG_STREAM_RETRY_COOLDOWN_MS": "15000",
        "TG_STREAM_MAX_CONSECUTIVE_PREVIEW_ERRORS": "2",
        "TG_STREAM_PREVIEW_FAILFAST": "1",
        "TG_FORMATTING_ENABLED": "1",
        "TG_FORMATTING_STYLE": "strong",
        "TG_FORMATTING_MODE": "html",
        "TG_FORMATTING_BACKEND": "telegramify",
        "TG_LINK_PREVIEW_POLICY": "auto",
        "TG_FORMATTING_FAIL_OPEN": "1",
    }


def load_config_snapshot(*, paths: ConfigPaths, secret_status: SecretStatus) -> dict[str, object]:
    env_values = default_env_values()
    current_values = read_env_file(paths.env_file)
    for key in SUPPORTED_ENV_KEYS:
        if key in current_values:
            env_values[key] = current_values[key]
    return {
        "env": env_values,
        "secrets": {
            "telegramToken": {
                "present": secret_status.present,
                "updatedAt": secret_status.updated_at,
                "backend": secret_status.backend,
                "available": secret_status.available,
            }
        },
    }


def _env_value(values: Mapping[str, str], key: str) -> str:
    value = values.get(key, "")
    return value.strip() if isinstance(value, str) else ""


def _is_runner_available(bin_name: str) -> bool:
    if "/" in bin_name:
        path = Path(bin_name).expanduser()
        return path.exists() and os.access(path, os.X_OK)
    return shutil.which(bin_name) is not None


def normalize_snapshot(payload: object) -> dict[str, str]:
    defaults = default_env_values()
    if not isinstance(payload, dict):
        return defaults
    env_payload = payload.get("env")
    if not isinstance(env_payload, dict):
        return defaults
    normalized = dict(defaults)
    for key in SUPPORTED_ENV_KEYS:
        raw = env_payload.get(key)
        if raw is None:
            continue
        normalized[key] = str(raw).strip()
    return normalized


def validate_snapshot(payload: object, *, secret_present: bool) -> dict[str, object]:
    env_values = normalize_snapshot(payload)
    errors: list[str] = []
    warnings: list[str] = []
    if not secret_present:
        warnings.append("Telegram bot token secret is not configured yet")

    user_ids = _env_value(env_values, "ALLOWED_TELEGRAM_USER_IDS")
    if user_ids:
        if not USER_IDS_RE.fullmatch(user_ids):
            errors.append("ALLOWED_TELEGRAM_USER_IDS format is invalid")
        else:
            try:
                parse_allowed_user_ids(user_ids)
            except ValueError as exc:
                errors.append(str(exc))

    default_cwd = Path(_env_value(env_values, "DEFAULT_CWD") or str(REPO_ROOT)).expanduser()
    if not default_cwd.exists() or not default_cwd.is_dir():
        errors.append(f"DEFAULT_CWD does not exist or is not a directory: {default_cwd}")

    allowed_roots = _env_value(env_values, "ALLOWED_CWD_ROOTS")
    try:
        parse_allowed_cwd_roots(allowed_roots)
    except Exception as exc:
        errors.append(f"ALLOWED_CWD_ROOTS is invalid: {exc}")

    try:
        parse_default_provider(_env_value(env_values, "DEFAULT_PROVIDER") or "codex")
    except ValueError as exc:
        errors.append(str(exc))

    try:
        parse_desktop_gpu_mode(_env_value(env_values, "TIYA_DESKTOP_GPU_MODE") or "disabled")
    except ValueError as exc:
        errors.append(str(exc))

    try:
        parse_dangerous_bypass_level(_env_value(env_values, "CODEX_DANGEROUS_BYPASS"))
    except ValueError as exc:
        errors.append(str(exc))

    parse_bool(_env_value(env_values, "TG_STREAM_ENABLED"), True)
    parse_non_negative_int(_env_value(env_values, "TG_STREAM_EDIT_INTERVAL_MS"), 700)
    parse_non_negative_int(_env_value(env_values, "TG_STREAM_MIN_DELTA_CHARS"), 8)
    parse_non_negative_int(_env_value(env_values, "TG_THINKING_STATUS_INTERVAL_MS"), 900)
    parse_non_negative_int(_env_value(env_values, "TG_HTTP_MAX_RETRIES"), 2)
    parse_non_negative_int(_env_value(env_values, "TG_HTTP_RETRY_BASE_MS"), 300)
    parse_non_negative_int(_env_value(env_values, "TG_HTTP_RETRY_MAX_MS"), 3000)
    parse_non_negative_int(_env_value(env_values, "TG_STREAM_RETRY_COOLDOWN_MS"), 15000)
    parse_positive_int(_env_value(env_values, "TG_STREAM_MAX_CONSECUTIVE_PREVIEW_ERRORS"), 2, minimum=1)
    parse_bool(_env_value(env_values, "TG_STREAM_PREVIEW_FAILFAST"), True)
    parse_bool(_env_value(env_values, "TG_FORMATTING_ENABLED"), True)
    parse_formatting_style(_env_value(env_values, "TG_FORMATTING_STYLE"))
    parse_formatting_mode(_env_value(env_values, "TG_FORMATTING_MODE"))
    parse_formatting_backend(_env_value(env_values, "TG_FORMATTING_BACKEND"))
    parse_link_preview_policy(_env_value(env_values, "TG_LINK_PREVIEW_POLICY"))
    parse_bool(_env_value(env_values, "TG_FORMATTING_FAIL_OPEN"), True)

    codex_bin = resolve_codex_bin(_env_value(env_values, "CODEX_BIN"))
    claude_bin = resolve_claude_bin(_env_value(env_values, "CLAUDE_BIN"))
    default_provider = _env_value(env_values, "DEFAULT_PROVIDER") or "codex"
    if not _is_runner_available(codex_bin):
        message = f"codex executable is unavailable: {codex_bin}"
        if default_provider == "codex":
            errors.append(message)
        else:
            warnings.append(message)
    if not _is_runner_available(claude_bin):
        message = f"claude executable is unavailable: {claude_bin}"
        if default_provider == "claude":
            errors.append(message)
        else:
            warnings.append(message)

    for key in ("CODEX_SESSION_ROOT", "CLAUDE_SESSION_ROOT"):
        raw = _env_value(env_values, key)
        if not raw:
            continue
        path = Path(raw).expanduser()
        if not path.exists():
            warnings.append(f"{key} does not exist yet: {path}")

    tg_proxy = _env_value(env_values, "TG_PROXY_URL")
    https_proxy = _env_value(env_values, "HTTPS_PROXY")
    http_proxy = _env_value(env_values, "HTTP_PROXY")
    if tg_proxy and (https_proxy or http_proxy) and any(value and value != tg_proxy for value in (https_proxy, http_proxy)):
        warnings.append("proxy settings disagree; TG_PROXY_URL will take precedence when the worker starts")

    return {
        "ok": not errors,
        "errors": errors,
        "warnings": warnings,
        "normalized": {"env": env_values},
    }


def persist_snapshot(*, paths: ConfigPaths, payload: object) -> dict[str, object]:
    env_values = normalize_snapshot(payload)
    write_env_file(paths.env_file, env_values, key_order=SUPPORTED_ENV_KEYS)
    return {"env": env_values}


def build_worker_env(*, base_environ: Mapping[str, str], token: str, env_values: Mapping[str, str], runtime_paths) -> dict[str, str]:
    worker_env = dict(base_environ)
    for key in SUPPORTED_ENV_KEYS:
        worker_env[key] = _env_value(env_values, key)
    worker_env["TELEGRAM_BOT_TOKEN"] = token
    worker_env["STORAGE_PATH"] = str(runtime_paths.db_file)
    worker_env["STATE_PATH"] = str(runtime_paths.state_file)
    worker_env["TG_INSTANCE_LOCK_PATH"] = str(runtime_paths.lock_base)

    # Normalize proxy inheritance so an explicitly empty desktop config does not
    # keep using lowercase proxy variables from the parent desktop environment.
    worker_env.pop("https_proxy", None)
    worker_env.pop("http_proxy", None)

    preferred_proxy = _env_value(env_values, "TG_PROXY_URL") or _env_value(env_values, "HTTPS_PROXY") or _env_value(
        env_values, "HTTP_PROXY"
    )
    if preferred_proxy:
        worker_env["TG_PROXY_URL"] = preferred_proxy
        worker_env["HTTPS_PROXY"] = preferred_proxy
        worker_env["https_proxy"] = preferred_proxy
        worker_env["HTTP_PROXY"] = preferred_proxy
        worker_env["http_proxy"] = preferred_proxy
    return worker_env
