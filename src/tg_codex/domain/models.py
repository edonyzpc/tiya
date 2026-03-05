from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass
class SessionMeta:
    session_id: str
    timestamp: str
    cwd: str
    file_path: str
    title: str


@dataclass
class CodexRunResult:
    thread_id: Optional[str]
    answer: str
    stderr_text: str
    return_code: int


@dataclass
class StreamSummary:
    stream_mode: str
    first_token_ms: int
    updates_total: int
    throttled_total: int
    fallback_triggered: bool
    final_send_ms: int
    exit_code: int


@dataclass(frozen=True)
class AppConfig:
    telegram_token: str
    telegram_proxy: Optional[str]
    allowed_user_ids: Optional[set[int]]
    stream_enabled: bool
    stream_edit_interval_ms: int
    stream_min_delta_chars: int
    thinking_status_interval_ms: int
    default_cwd: Path
    state_path: Path
    session_root: Path
    codex_bin: str
    dangerous_bypass_level: int
    codex_sandbox_mode: Optional[str]
    codex_approval_policy: Optional[str]
    tg_http_max_retries: int
    tg_http_retry_base_ms: int
    tg_http_retry_max_ms: int
