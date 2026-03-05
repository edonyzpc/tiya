import os
import shutil
from pathlib import Path
from typing import Optional, cast

from domain.models import (
    AgentProvider,
    AppConfig,
    FormattingBackend,
    FormattingMode,
    FormattingStyle,
    LinkPreviewPolicy,
)


def env(name: str, default: Optional[str] = None) -> Optional[str]:
    value = os.getenv(name)
    if value is None:
        return default
    value = value.strip()
    return value if value else default


def parse_allowed_user_ids(raw: Optional[str]) -> Optional[set[int]]:
    if not raw:
        return None
    result: set[int] = set()
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            result.add(int(part))
        except ValueError as exc:
            raise ValueError(f"invalid user id in ALLOWED_TELEGRAM_USER_IDS: {part}") from exc
    return result


def parse_dangerous_bypass_level(raw: Optional[str]) -> int:
    value = (raw or "0").strip()
    if not value:
        return 0
    try:
        level = int(value)
    except ValueError as exc:
        raise ValueError("CODEX_DANGEROUS_BYPASS must be 0, 1, or 2") from exc
    if level < 0:
        level = 0
    if level > 2:
        level = 2
    return level


def parse_default_provider(raw: Optional[str]) -> AgentProvider:
    value = (raw or "codex").strip().lower()
    if value not in ("codex", "claude"):
        raise ValueError("DEFAULT_PROVIDER must be codex or claude")
    return cast(AgentProvider, value)


def parse_non_negative_int(raw: Optional[str], default: int) -> int:
    if raw is None:
        return default
    try:
        value = int(raw.strip())
    except (ValueError, TypeError, AttributeError):
        return default
    return value if value >= 0 else default


def parse_positive_int(raw: Optional[str], default: int, minimum: int = 1) -> int:
    if raw is None:
        return max(minimum, default)
    try:
        value = int(raw.strip())
    except (ValueError, TypeError, AttributeError):
        return max(minimum, default)
    return value if value >= minimum else max(minimum, default)


def parse_bool(raw: Optional[str], default: bool) -> bool:
    if raw is None:
        return default
    value = raw.strip().lower()
    if not value:
        return default
    if value in ("1", "true", "yes", "y", "on"):
        return True
    if value in ("0", "false", "no", "n", "off"):
        return False
    return default


def parse_formatting_style(raw: Optional[str]) -> FormattingStyle:
    value = (raw or "strong").strip().lower()
    if value not in ("light", "medium", "strong"):
        value = "strong"
    return cast(FormattingStyle, value)


def parse_formatting_mode(raw: Optional[str]) -> FormattingMode:
    value = (raw or "html").strip().lower()
    if value not in ("html", "plain"):
        value = "html"
    return cast(FormattingMode, value)


def parse_link_preview_policy(raw: Optional[str]) -> LinkPreviewPolicy:
    value = (raw or "auto").strip().lower()
    if value not in ("auto", "off"):
        value = "auto"
    return cast(LinkPreviewPolicy, value)


def parse_formatting_backend(raw: Optional[str]) -> FormattingBackend:
    value = (raw or "builtin").strip().lower()
    if value not in ("builtin", "telegramify", "sulguk"):
        value = "builtin"
    return cast(FormattingBackend, value)


def resolve_tg_stream_enabled() -> bool:
    explicit = os.getenv("TG_STREAM_ENABLED")
    if explicit is not None and explicit.strip():
        return explicit.strip() != "0"
    legacy = os.getenv("TELEGRAM_ENABLE_DRAFT_STREAM")
    if legacy is not None and legacy.strip():
        return legacy.strip() != "0"
    return True


def resolve_codex_bin(configured: Optional[str]) -> str:
    if configured:
        return configured
    found = shutil.which("codex")
    if found:
        return found
    app_path = "/Applications/Codex.app/Contents/Resources/codex"
    if Path(app_path).exists():
        return app_path
    return "codex"


def resolve_claude_bin(configured: Optional[str]) -> str:
    if configured:
        return configured
    found = shutil.which("claude")
    if found:
        return found
    default_path = Path("~/.local/bin/claude").expanduser()
    if default_path.exists():
        return str(default_path)
    return "claude"


def resolve_tg_proxy() -> Optional[str]:
    explicit = env("TG_PROXY_URL")
    if explicit:
        return explicit
    for key in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
        value = os.getenv(key)
        if value and value.strip():
            return value.strip()
    return None


def load_config() -> AppConfig:
    token = env("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("missing TELEGRAM_BOT_TOKEN")

    return AppConfig(
        telegram_token=token,
        telegram_proxy=resolve_tg_proxy(),
        allowed_user_ids=parse_allowed_user_ids(env("ALLOWED_TELEGRAM_USER_IDS")),
        default_provider=parse_default_provider(env("DEFAULT_PROVIDER", "codex")),
        stream_enabled=resolve_tg_stream_enabled(),
        stream_edit_interval_ms=parse_non_negative_int(env("TG_STREAM_EDIT_INTERVAL_MS", "700"), 700),
        stream_min_delta_chars=parse_non_negative_int(env("TG_STREAM_MIN_DELTA_CHARS", "8"), 8),
        thinking_status_interval_ms=parse_non_negative_int(env("TG_THINKING_STATUS_INTERVAL_MS", "900"), 900),
        default_cwd=Path(env("DEFAULT_CWD", os.getcwd())).expanduser(),
        state_path=Path(env("STATE_PATH", "./bot_state.json")),
        codex_session_root=Path(env("CODEX_SESSION_ROOT", "~/.codex/sessions")).expanduser(),
        claude_session_root=Path(env("CLAUDE_SESSION_ROOT", "~/.claude/projects")).expanduser(),
        codex_bin=resolve_codex_bin(env("CODEX_BIN")),
        claude_bin=resolve_claude_bin(env("CLAUDE_BIN")),
        claude_model=env("CLAUDE_MODEL"),
        claude_permission_mode=env("CLAUDE_PERMISSION_MODE", "default") or "default",
        dangerous_bypass_level=parse_dangerous_bypass_level(env("CODEX_DANGEROUS_BYPASS", "0")),
        codex_sandbox_mode=env("CODEX_SANDBOX_MODE"),
        codex_approval_policy=env("CODEX_APPROVAL_POLICY"),
        tg_http_max_retries=parse_non_negative_int(env("TG_HTTP_MAX_RETRIES", "2"), 2),
        tg_http_retry_base_ms=parse_non_negative_int(env("TG_HTTP_RETRY_BASE_MS", "300"), 300),
        tg_http_retry_max_ms=parse_non_negative_int(env("TG_HTTP_RETRY_MAX_MS", "3000"), 3000),
        tg_instance_lock_path=Path(env("TG_INSTANCE_LOCK_PATH", "./.runtime/bot.lock")).expanduser(),
        tg_stream_retry_cooldown_ms=parse_non_negative_int(
            env("TG_STREAM_RETRY_COOLDOWN_MS", "15000"),
            15000,
        ),
        tg_stream_max_consecutive_preview_errors=parse_positive_int(
            env("TG_STREAM_MAX_CONSECUTIVE_PREVIEW_ERRORS", "2"),
            default=2,
            minimum=1,
        ),
        tg_stream_preview_failfast=parse_bool(env("TG_STREAM_PREVIEW_FAILFAST", "1"), True),
        tg_formatting_enabled=parse_bool(env("TG_FORMATTING_ENABLED", "1"), True),
        tg_formatting_final_only=parse_bool(env("TG_FORMATTING_FINAL_ONLY", "1"), True),
        tg_formatting_style=parse_formatting_style(env("TG_FORMATTING_STYLE", "strong")),
        tg_formatting_mode=parse_formatting_mode(env("TG_FORMATTING_MODE", "html")),
        tg_link_preview_policy=parse_link_preview_policy(env("TG_LINK_PREVIEW_POLICY", "auto")),
        tg_formatting_fail_open=parse_bool(env("TG_FORMATTING_FAIL_OPEN", "1"), True),
        tg_formatting_backend=parse_formatting_backend(env("TG_FORMATTING_BACKEND", "builtin")),
    )
