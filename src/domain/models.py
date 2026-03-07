from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Optional


AgentProvider = Literal["codex", "claude"]
FormattingStyle = Literal["light", "medium", "strong"]
FormattingMode = Literal["html", "plain"]
LinkPreviewPolicy = Literal["auto", "off"]
FormattingBackend = Literal["builtin", "telegramify", "sulguk"]
InteractionKind = Literal["approval", "question"]
InteractionReplyMode = Literal["buttons", "text"]
ApprovalDecision = Literal["accept", "acceptForSession", "decline", "cancel"]


@dataclass
class SessionMeta:
    session_id: str
    timestamp: str
    cwd: str
    file_path: str
    title: str


@dataclass
class AgentRunResult:
    thread_id: Optional[str]
    answer: str
    stderr_text: str
    return_code: int


@dataclass(frozen=True)
class PromptImage:
    path: Path
    file_name: str
    mime_type: Optional[str]
    file_size: Optional[int]
    attachment_ref_id: Optional[int] = None


@dataclass(frozen=True)
class PendingImage:
    path: Path
    file_name: str
    mime_type: Optional[str]
    file_size: Optional[int]
    message_id: int
    created_at: int
    attachment_ref_id: Optional[int] = None

    def to_prompt_image(self) -> PromptImage:
        return PromptImage(
            path=self.path,
            file_name=self.file_name,
            mime_type=self.mime_type,
            file_size=self.file_size,
            attachment_ref_id=self.attachment_ref_id,
        )


@dataclass(frozen=True)
class InteractionOption:
    id: str
    label: str
    description: str = ""


@dataclass(frozen=True)
class PendingInteraction:
    interaction_id: str
    run_id: str
    kind: InteractionKind
    title: str
    body: str
    options: tuple[InteractionOption, ...]
    reply_mode: InteractionReplyMode
    created_at: int
    expires_at: int
    chat_id: int
    message_id: Optional[int] = None

    def to_dict(self) -> dict[str, object]:
        return {
            "interaction_id": self.interaction_id,
            "run_id": self.run_id,
            "kind": self.kind,
            "title": self.title,
            "body": self.body,
            "options": [
                {
                    "id": option.id,
                    "label": option.label,
                    "description": option.description,
                }
                for option in self.options
            ],
            "reply_mode": self.reply_mode,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "chat_id": self.chat_id,
            "message_id": self.message_id,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> Optional["PendingInteraction"]:
        interaction_id = payload.get("interaction_id")
        run_id = payload.get("run_id")
        kind = payload.get("kind")
        title = payload.get("title")
        body = payload.get("body")
        reply_mode = payload.get("reply_mode")
        created_at = payload.get("created_at")
        expires_at = payload.get("expires_at")
        chat_id = payload.get("chat_id")
        if not all(
            (
                isinstance(interaction_id, str) and interaction_id,
                isinstance(run_id, str) and run_id,
                isinstance(kind, str) and kind in ("approval", "question"),
                isinstance(title, str),
                isinstance(body, str),
                isinstance(reply_mode, str) and reply_mode in ("buttons", "text"),
                isinstance(created_at, int),
                isinstance(expires_at, int),
                isinstance(chat_id, int),
            )
        ):
            return None

        raw_options = payload.get("options")
        options: list[InteractionOption] = []
        if isinstance(raw_options, list):
            for raw_option in raw_options:
                if not isinstance(raw_option, dict):
                    continue
                option_id = raw_option.get("id")
                label = raw_option.get("label")
                description = raw_option.get("description")
                if not isinstance(option_id, str) or not option_id:
                    continue
                if not isinstance(label, str) or not label:
                    continue
                if description is not None and not isinstance(description, str):
                    description = ""
                options.append(
                    InteractionOption(
                        id=option_id,
                        label=label,
                        description=description or "",
                    )
                )

        message_id = payload.get("message_id")
        if message_id is not None and not isinstance(message_id, int):
            message_id = None

        return cls(
            interaction_id=interaction_id,
            run_id=run_id,
            kind=kind,
            title=title,
            body=body,
            options=tuple(options),
            reply_mode=reply_mode,
            created_at=created_at,
            expires_at=expires_at,
            chat_id=chat_id,
            message_id=message_id,
        )


@dataclass(frozen=True)
class ActiveRunState:
    run_id: str
    chat_id: int
    chat_type: str
    started_at: int

    def to_dict(self) -> dict[str, object]:
        return {
            "run_id": self.run_id,
            "chat_id": self.chat_id,
            "chat_type": self.chat_type,
            "started_at": self.started_at,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> Optional["ActiveRunState"]:
        run_id = payload.get("run_id")
        chat_id = payload.get("chat_id")
        chat_type = payload.get("chat_type")
        started_at = payload.get("started_at")
        if not (
            isinstance(run_id, str)
            and run_id
            and isinstance(chat_id, int)
            and isinstance(chat_type, str)
            and chat_type
            and isinstance(started_at, int)
        ):
            return None
        return cls(
            run_id=run_id,
            chat_id=chat_id,
            chat_type=chat_type,
            started_at=started_at,
        )


@dataclass(frozen=True)
class ApprovalRequest:
    kind: Literal["command", "file_change"]
    title: str
    body: str
    command: Optional[str] = None
    cwd: Optional[str] = None
    allow_accept_for_session: bool = False


@dataclass(frozen=True)
class QuestionRequest:
    title: str
    body: str
    options: tuple[InteractionOption, ...]
    reply_mode: InteractionReplyMode = "buttons"


# Backward-compatible alias for existing imports.
CodexRunResult = AgentRunResult


@dataclass
class StreamSummary:
    stream_mode: str
    first_token_ms: int
    updates_total: int
    throttled_total: int
    fallback_triggered: bool
    preview_errors_total: int
    retry_after_total: int
    degraded_reason: str
    degraded_at_ms: int
    final_send_ms: int
    exit_code: int


@dataclass(frozen=True)
class StreamConfig:
    enabled: bool
    edit_interval_ms: int
    min_delta_chars: int
    thinking_status_interval_ms: int
    retry_cooldown_ms: int
    max_consecutive_preview_errors: int
    preview_failfast: bool


@dataclass(frozen=True)
class AppConfig:
    telegram_token: str
    telegram_proxy: Optional[str]
    allowed_user_ids: Optional[set[int]]
    allowed_cwd_roots: tuple[Path, ...]
    default_provider: AgentProvider
    stream_enabled: bool
    stream_edit_interval_ms: int
    stream_min_delta_chars: int
    thinking_status_interval_ms: int
    default_cwd: Path
    storage_path: Path
    legacy_state_path: Path
    codex_session_root: Path
    claude_session_root: Path
    codex_bin: str
    claude_bin: str
    claude_model: Optional[str]
    claude_permission_mode: str
    dangerous_bypass_level: int
    codex_sandbox_mode: Optional[str]
    codex_approval_policy: Optional[str]
    tg_http_max_retries: int
    tg_http_retry_base_ms: int
    tg_http_retry_max_ms: int
    tg_instance_lock_path: Path
    tg_stream_retry_cooldown_ms: int
    tg_stream_max_consecutive_preview_errors: int
    tg_stream_preview_failfast: bool
    tg_formatting_enabled: bool
    tg_formatting_final_only: bool
    tg_formatting_style: FormattingStyle
    tg_formatting_mode: FormattingMode
    tg_link_preview_policy: LinkPreviewPolicy
    tg_formatting_fail_open: bool
    tg_formatting_backend: FormattingBackend
