import asyncio
import hashlib
import os
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from aiogram import Router
from aiogram.types import CallbackQuery, Document, InlineKeyboardButton, InlineKeyboardMarkup, Message, PhotoSize

from ..domain.models import (
    ApprovalDecision,
    ApprovalRequest,
    AgentProvider,
    InteractionOption,
    PendingInteraction,
    PendingImage,
    PromptImage,
    QuestionRequest,
    StreamConfig,
)
from ..logging_utils import log
from ..services.interaction_coordinator import InteractionCoordinator
from ..services.runner_protocol import RunnerProtocol
from ..services.session_store import AsyncSessionStoreProtocol, CodexSessionStore
from ..services.state_store import StateStore
from .client import TelegramClient
from .render_dispatch import send_render_result
from .rendering import RenderProfile, TelegramMessageRenderer
from .streaming import StreamOrchestrator, TypingStatus

BOT_COMMANDS: list[dict[str, str]] = [
    {"command": "start", "description": "开始使用"},
    {"command": "help", "description": "查看帮助"},
    {"command": "provider", "description": "切换模型提供方"},
    {"command": "sessions", "description": "查看最近会话"},
    {"command": "use", "description": "切换会话"},
    {"command": "history", "description": "查看会话历史"},
    {"command": "new", "description": "新建会话模式"},
    {"command": "status", "description": "查看当前会话"},
    {"command": "ask", "description": "在当前会话提问"},
    {"command": "cancel", "description": "取消当前执行"},
]

MAX_TELEGRAM_IMAGE_BYTES = 20 * 1024 * 1024
_IMAGE_DOCUMENT_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}
_MIME_EXTENSION_OVERRIDES = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "image/bmp": ".bmp",
}


@dataclass(frozen=True)
class IncomingImage:
    file_id: str
    file_unique_id: str
    file_name: str
    mime_type: Optional[str]
    file_size: Optional[int]


@dataclass(frozen=True)
class RunInteractionBridge:
    service: "TgCodexService"
    chat_id: int
    reply_to: int
    user_id: int
    provider: AgentProvider
    chat_type: str
    orchestrator: Optional[StreamOrchestrator] = None
    typing: Optional[TypingStatus] = None

    async def request_approval(self, request: ApprovalRequest) -> ApprovalDecision:
        return await self.service._request_approval(
            chat_id=self.chat_id,
            reply_to=self.reply_to,
            user_id=self.user_id,
            provider=self.provider,
            chat_type=self.chat_type,
            request=request,
            orchestrator=self.orchestrator,
            typing=self.typing,
        )

    async def request_question(self, request: QuestionRequest) -> Optional[list[str]]:
        return await self.service._request_question(
            chat_id=self.chat_id,
            reply_to=self.reply_to,
            user_id=self.user_id,
            provider=self.provider,
            chat_type=self.chat_type,
            request=request,
            orchestrator=self.orchestrator,
            typing=self.typing,
        )


class TgCodexService:
    def __init__(
        self,
        api: TelegramClient,
        session_stores: dict[AgentProvider, AsyncSessionStoreProtocol],
        state: StateStore,
        runners: dict[AgentProvider, RunnerProtocol],
        runner_bins: dict[AgentProvider, str],
        default_cwd: Path,
        attachments_root: Path,
        allowed_user_ids: Optional[set[int]],
        allowed_cwd_roots: tuple[Path, ...],
        stream_config: StreamConfig,
        interaction_timeout_sec: int = 600,
        renderer: Optional[TelegramMessageRenderer] = None,
    ):
        self.api = api
        self.session_stores = session_stores
        self.state = state
        self.runners = runners
        self.runner_bins = runner_bins
        self.default_cwd = default_cwd
        self.attachments_root = attachments_root.expanduser()
        self.allowed_user_ids = allowed_user_ids
        self.allowed_cwd_roots = tuple(path.expanduser().resolve() for path in allowed_cwd_roots)
        self.stream_config = stream_config
        self.interaction_timeout_sec = max(60, int(interaction_timeout_sec))
        self.renderer = renderer
        self.interactions = InteractionCoordinator(state)
        self._background_tasks: list[asyncio.Task[None]] = []

    async def shutdown(self) -> None:
        for task in self._background_tasks:
            if task.done():
                continue
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        await self.state.close()

    def start_background_refresh(self) -> None:
        for provider, store in self.session_stores.items():
            refresh_all = getattr(store, "refresh_all", None)
            if refresh_all is None:
                continue

            async def _runner(provider_name: AgentProvider, fn) -> None:
                try:
                    await fn()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    log(f"[warn] background session refresh failed (provider={provider_name}, error={exc!r})")

            self._background_tasks.append(asyncio.create_task(_runner(provider, refresh_all)))

    @staticmethod
    def _text_fingerprint(text: str) -> str:
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]
        return f"len={len(text)} sha={digest}"

    async def setup_bot_menu(self) -> None:
        # Write command menu for default + common language overrides.
        await self.api.set_my_commands(BOT_COMMANDS)
        for lang in ("zh", "en"):
            try:
                await self.api.set_my_commands(BOT_COMMANDS, language_code=lang)
            except Exception:
                pass
        try:
            await self.api.set_chat_menu_button_commands()
        except Exception:
            pass

    async def handle_callback_query(self, callback_query: CallbackQuery) -> None:
        cq_id = callback_query.id
        data = (callback_query.data or "").strip()
        msg = callback_query.message
        chat_id = getattr(getattr(msg, "chat", None), "id", None)
        message_id = getattr(msg, "message_id", None)
        user = callback_query.from_user
        user_id = getattr(user, "id", None)

        if not cq_id or user_id is None:
            return
        if self.allowed_user_ids is not None and int(user_id) not in self.allowed_user_ids:
            await self.api.answer_callback_query(cq_id, text="没有权限。", show_alert=True)
            return
        if not isinstance(chat_id, int):
            await self.api.answer_callback_query(cq_id, text="无法解析聊天上下文。", show_alert=True)
            return

        if data.startswith("use:"):
            provider, session_id = await self._parse_use_callback_data(int(user_id), data)
            await self.api.answer_callback_query(cq_id, text="正在切换会话...")
            switched = await self._switch_to_session(chat_id, message_id, int(user_id), provider, session_id)
            if switched and isinstance(message_id, int):
                await self._clear_message_markup(chat_id, message_id)
            return

        if data.startswith("ixa:"):
            parts = data.split(":", 3)
            if len(parts) == 4 and parts[1] in ("codex", "claude"):
                ok = await self.interactions.resolve_option(
                    user_id=int(user_id),
                    provider=parts[1],  # type: ignore[arg-type]
                    chat_id=chat_id,
                    interaction_id=parts[2],
                    option_id=parts[3],
                )
                await self.api.answer_callback_query(
                    cq_id,
                    text="已提交。" if ok else "这个确认已失效。",
                    show_alert=not ok,
                )
                if ok and isinstance(message_id, int):
                    await self._clear_message_markup(chat_id, message_id)
                return

        if data.startswith("ixq:"):
            parts = data.split(":", 3)
            if len(parts) == 4 and parts[1] in ("codex", "claude"):
                ok = await self.interactions.resolve_option(
                    user_id=int(user_id),
                    provider=parts[1],  # type: ignore[arg-type]
                    chat_id=chat_id,
                    interaction_id=parts[2],
                    option_id=parts[3],
                )
                await self.api.answer_callback_query(
                    cq_id,
                    text="已提交。" if ok else "这个问题已失效。",
                    show_alert=not ok,
                )
                if ok and isinstance(message_id, int):
                    await self._clear_message_markup(chat_id, message_id)
                return

        await self.api.answer_callback_query(cq_id, text="不支持的操作。", show_alert=True)

    async def handle_message(self, msg: Message) -> None:
        chat = msg.chat
        chat_id = chat.id
        chat_type = str(chat.type)
        message_id = msg.message_id
        user = msg.from_user
        user_id = getattr(user, "id", None)
        text = (msg.text or "").strip()

        if user_id is None:
            return

        log(f"update received: user_id={user_id} chat_id={chat_id} text_{self._text_fingerprint(text)}")
        if self.allowed_user_ids is not None and int(user_id) not in self.allowed_user_ids:
            log(f"blocked by allowlist: user_id={user_id}")
            await self.api.send_message(chat_id, "没有权限使用这个 bot。", reply_to=message_id)
            return

        provider = await self.state.get_active_provider(int(user_id))
        incoming_image = self._extract_incoming_image(msg)
        if incoming_image is not None:
            await self.state.set_pending_session_pick(int(user_id), False, provider=provider)
            await self._handle_image_message(
                chat_id=chat_id,
                reply_to=message_id,
                user_id=int(user_id),
                provider=provider,
                image=incoming_image,
                caption=(msg.caption or "").strip(),
                media_group_id=getattr(msg, "media_group_id", None),
                chat_type=chat_type,
            )
            return

        if isinstance(msg.document, Document):
            await self.api.send_message(
                chat_id,
                "当前只支持图片文件作为上下文，请发送单张图片。",
                reply_to=message_id,
            )
            return

        if not text:
            return

        if not text.startswith("/"):
            if await self._try_handle_pending_question_reply(chat_id, message_id, int(user_id), provider, text):
                return
            if await self._try_handle_quick_session_pick(chat_id, message_id, int(user_id), provider, text):
                return
            if await self._try_handle_pending_image_prompt(chat_id, message_id, int(user_id), provider, text, chat_type):
                return
            if await self._reject_when_active_run(chat_id, message_id, int(user_id), provider):
                return
            await self.state.set_pending_session_pick(int(user_id), False, provider=provider)
            await self._handle_chat_message(chat_id, message_id, int(user_id), provider, text, chat_type)
            return

        cmd, arg = self._parse_command(text)
        log(f"command: /{cmd} arg_{self._text_fingerprint(arg)}")

        if cmd in ("start", "help"):
            await self._send_help(chat_id, message_id)
            return
        if cmd == "provider":
            await self._handle_provider(chat_id, message_id, int(user_id), arg)
            return
        if cmd == "sessions":
            await self._handle_sessions(chat_id, message_id, int(user_id), provider, arg)
            return
        if cmd == "use":
            await self._handle_use(chat_id, message_id, int(user_id), provider, arg)
            return
        if cmd == "status":
            await self._handle_status(chat_id, message_id, int(user_id), provider)
            return
        if cmd == "cancel":
            await self._handle_cancel(chat_id, message_id, int(user_id), provider)
            return
        if cmd == "history":
            await self._handle_history(chat_id, message_id, int(user_id), provider, arg)
            return
        if await self._reject_when_active_run(chat_id, message_id, int(user_id), provider):
            return
        if cmd == "new":
            await self._handle_new(chat_id, message_id, int(user_id), provider, arg)
            return
        if cmd == "ask":
            if arg.strip() and await self._try_handle_pending_image_prompt(
                chat_id,
                message_id,
                int(user_id),
                provider,
                arg,
                chat_type,
            ):
                return
            await self._handle_ask(chat_id, message_id, int(user_id), provider, arg, chat_type)
            return

        await self.api.send_message(chat_id, f"未知命令: /{cmd}\n发送 /help 查看说明。", reply_to=message_id)

    @staticmethod
    def _parse_command(text: str) -> tuple[str, str]:
        parts = text.split(" ", 1)
        cmd = parts[0][1:]
        cmd = cmd.split("@", 1)[0].strip().lower()
        arg = parts[1].strip() if len(parts) > 1 else ""
        return cmd, arg

    async def _parse_use_callback_data(self, user_id: int, data: str) -> tuple[AgentProvider, str]:
        # New format: use:<provider>:<session_id>
        parts = data.split(":", 2)
        if len(parts) == 3 and parts[1] in ("codex", "claude"):
            return parts[1], parts[2]
        return await self.state.get_active_provider(user_id), data[4:]

    @staticmethod
    def _extract_incoming_image(msg: Message) -> Optional[IncomingImage]:
        if msg.photo:
            largest = max(
                msg.photo,
                key=lambda item: ((item.file_size or 0), item.width * item.height),
            )
            if not isinstance(largest, PhotoSize):
                return None
            return IncomingImage(
                file_id=largest.file_id,
                file_unique_id=largest.file_unique_id,
                file_name=f"telegram-photo-{largest.file_unique_id}.jpg",
                mime_type="image/jpeg",
                file_size=largest.file_size,
            )

        document = msg.document
        if not isinstance(document, Document):
            return None
        if not TgCodexService._is_supported_image_document(document):
            return None
        file_name = TgCodexService._document_file_name(document)
        return IncomingImage(
            file_id=document.file_id,
            file_unique_id=document.file_unique_id,
            file_name=file_name,
            mime_type=document.mime_type,
            file_size=document.file_size,
        )

    @staticmethod
    def _is_supported_image_document(document: Document) -> bool:
        mime_type = (document.mime_type or "").strip().lower()
        if mime_type.startswith("image/"):
            return True
        suffix = Path(document.file_name or "").suffix.lower()
        return suffix in _IMAGE_DOCUMENT_SUFFIXES

    @staticmethod
    def _document_file_name(document: Document) -> str:
        original_name = Path(document.file_name or "").name.strip()
        suffix = Path(original_name).suffix.lower()
        if suffix not in _IMAGE_DOCUMENT_SUFFIXES:
            suffix = _MIME_EXTENSION_OVERRIDES.get((document.mime_type or "").strip().lower(), ".img")
        if original_name:
            stem = Path(original_name).stem or f"telegram-document-{document.file_unique_id}"
            return f"{stem}{suffix}"
        return f"telegram-document-{document.file_unique_id}{suffix}"

    def _attachment_path(self, chat_id: int, user_id: int, message_id: int, file_name: str) -> Path:
        safe_name = Path(file_name).name or "attachment.img"
        return self.attachments_root / f"user-{user_id}" / f"chat-{chat_id}" / f"msg-{message_id}" / safe_name

    async def _download_prompt_image(
        self,
        chat_id: int,
        user_id: int,
        message_id: int,
        image: IncomingImage,
    ) -> PendingImage:
        destination = self._attachment_path(chat_id, user_id, message_id, image.file_name)
        await self.api.download_telegram_file(image.file_id, destination)
        attachment_ref_id = await self.state.storage.store_attachment_file(
            destination,
            file_name=image.file_name,
            mime_type=image.mime_type,
            file_size=image.file_size,
            source_kind="telegram_image",
        )
        return PendingImage(
            path=destination,
            file_name=image.file_name,
            mime_type=image.mime_type,
            file_size=image.file_size,
            message_id=message_id,
            created_at=int(time.time()),
            attachment_ref_id=attachment_ref_id,
        )

    async def _clear_pending_image_and_files(
        self,
        user_id: int,
        provider: AgentProvider,
    ) -> Optional[PendingImage]:
        pending = await self.state.clear_pending_image(user_id, provider=provider)
        if pending is not None:
            self._delete_attachment_dir(pending.path.parent)
        return pending

    @staticmethod
    def _delete_attachment_dir(path: Path) -> None:
        try:
            shutil.rmtree(path, ignore_errors=True)
        except Exception:
            pass

    @staticmethod
    def _interaction_callback_prefix(kind: str) -> str:
        return "ixa" if kind == "approval" else "ixq"

    @staticmethod
    def _approval_options(allow_accept_for_session: bool) -> tuple[InteractionOption, ...]:
        options = [
            InteractionOption(id="accept", label="批准一次"),
        ]
        if allow_accept_for_session:
            options.append(InteractionOption(id="acceptForSession", label="本会话放行"))
        options.extend(
            [
                InteractionOption(id="decline", label="拒绝继续"),
                InteractionOption(id="cancel", label="取消本轮"),
            ]
        )
        return tuple(options)

    def _interaction_markup(
        self,
        *,
        provider: AgentProvider,
        interaction_id: str,
        kind: str,
        options: tuple[InteractionOption, ...],
    ) -> InlineKeyboardMarkup:
        prefix = self._interaction_callback_prefix(kind)
        rows = [
            [
                InlineKeyboardButton(
                    text=option.label,
                    callback_data=f"{prefix}:{provider}:{interaction_id}:{option.id}",
                )
            ]
            for option in options
        ]
        return InlineKeyboardMarkup(inline_keyboard=rows)

    @staticmethod
    def _compose_interaction_text(
        title: str,
        body: str,
        *,
        note: Optional[str] = None,
        options: tuple[InteractionOption, ...] = (),
    ) -> str:
        lines = [title.strip() or "需要处理交互"]
        if body.strip():
            lines.extend(["", body.strip()])
        if options and any(option.description.strip() for option in options):
            lines.extend(["", "可选项："])
            for option in options:
                description = option.description.strip()
                if description:
                    lines.append(f"- {option.label}: {description}")
        if note:
            lines.extend(["", note.strip()])
        return "\n".join(lines).strip()

    def _compose_closed_interaction_text(
        self,
        interaction: PendingInteraction,
        *,
        status: str,
    ) -> str:
        base = self._compose_interaction_text(
            interaction.title,
            interaction.body,
            options=interaction.options,
        )
        return f"{base}\n\n状态: {status.strip()}".strip()

    async def _clear_message_markup(self, chat_id: int, message_id: int) -> None:
        try:
            await self.api.edit_message_reply_markup(chat_id=chat_id, message_id=message_id, reply_markup=None)
        except Exception as exc:
            log(
                "[warn] clear inline keyboard failed "
                f"(chat_id={chat_id}, message_id={message_id}, error={exc!r})"
            )

    async def _close_interaction_message(
        self,
        interaction: PendingInteraction,
        *,
        status: str,
    ) -> None:
        if interaction.message_id is None:
            return
        try:
            await self.api.edit_message_text(
                chat_id=interaction.chat_id,
                message_id=interaction.message_id,
                text=self._compose_closed_interaction_text(interaction, status=status),
                reply_markup=None,
            )
        except Exception as exc:
            log(
                "[warn] close interaction message failed "
                f"(chat_id={interaction.chat_id}, message_id={interaction.message_id}, error={exc!r})"
            )
            await self._clear_message_markup(interaction.chat_id, interaction.message_id)

    async def _try_handle_pending_question_reply(
        self,
        chat_id: int,
        reply_to: int,
        user_id: int,
        provider: AgentProvider,
        text: str,
    ) -> bool:
        pending = await self.interactions.get_pending_interaction(user_id, provider)
        if pending is None or pending.kind != "question" or pending.reply_mode != "text":
            return False
        if pending.chat_id != chat_id:
            return False
        resolved = await self.interactions.resolve_text_reply(
            user_id=user_id,
            provider=provider,
            chat_id=chat_id,
            text=text,
        )
        if resolved:
            await self.api.send_message(chat_id, "已收到回复，继续处理中。", reply_to=reply_to)
        return resolved

    async def _reject_when_active_run(
        self,
        chat_id: int,
        reply_to: int,
        user_id: int,
        provider: AgentProvider,
    ) -> bool:
        active_run = await self.interactions.get_active_run(user_id, provider)
        if active_run is None:
            return False

        pending = await self.interactions.get_pending_interaction(user_id, provider)
        if pending is not None and pending.chat_id == chat_id:
            if pending.kind == "approval":
                message = "当前执行正在等待确认，请先点按钮处理，或发送 /cancel。"
            elif pending.reply_mode == "text":
                message = "当前执行正在等待你的文字回复，请直接发送答案，或发送 /cancel。"
            else:
                message = "当前执行正在等待问题选择，请先点按钮处理，或发送 /cancel。"
        elif active_run.chat_id != chat_id:
            message = "当前 provider 正在另一个聊天里执行，请先在原聊天处理完成，或发送 /cancel。"
        else:
            message = "当前 provider 正在执行中，请等待完成或发送 /cancel。"

        await self.api.send_message(chat_id, message, reply_to=reply_to)
        return True

    async def _handle_cancel(
        self,
        chat_id: int,
        reply_to: int,
        user_id: int,
        provider: AgentProvider,
    ) -> None:
        cancelled = await self.interactions.cancel_run(user_id, provider)
        if not cancelled:
            await self.api.send_message(chat_id, "当前没有可取消的执行。", reply_to=reply_to)
            return
        await self.api.send_message(chat_id, "已请求取消当前执行。", reply_to=reply_to)

    async def _request_approval(
        self,
        *,
        chat_id: int,
        reply_to: int,
        user_id: int,
        provider: AgentProvider,
        chat_type: str,
        request: ApprovalRequest,
        orchestrator: Optional[StreamOrchestrator] = None,
        typing: Optional[TypingStatus] = None,
    ) -> ApprovalDecision:
        if chat_type != "private":
            await self.api.send_message(
                chat_id,
                "当前执行需要人工确认，但群聊/频道里暂不支持审批交互。请在私聊中重试。",
                reply_to=reply_to,
            )
            return "cancel"

        body_lines = [request.body.strip()] if request.body.strip() else []
        if request.command:
            body_lines.append(f"命令: {request.command}")
        if request.cwd:
            body_lines.append(f"目录: {request.cwd}")
        options = self._approval_options(request.allow_accept_for_session)
        status_text = "已结束"
        try:
            if orchestrator is not None:
                orchestrator.pause_for_interaction()
            if typing is not None:
                typing.pause()
            try:
                waiter = await self.interactions.open_interaction(
                    user_id=user_id,
                    provider=provider,
                    kind="approval",
                    title=request.title,
                    body="\n".join(body_lines).strip(),
                    options=options,
                    reply_mode="buttons",
                    chat_id=chat_id,
                    message_id=None,
                    timeout_sec=self.interaction_timeout_sec,
                )
            except RuntimeError as exc:
                log(
                    "[warn] approval interaction ignored because active run is missing "
                    f"(provider={provider}, user_id={user_id}, error={exc!r})"
                )
                return "cancel"

            try:
                message = self._compose_interaction_text(
                    request.title,
                    "\n".join(body_lines),
                    note="请在 10 分钟内处理，或发送 /cancel。",
                    options=options,
                )
                sent = await self.api.send_message_with_result(
                    chat_id=chat_id,
                    text=message,
                    reply_to=reply_to,
                    reply_markup=self._interaction_markup(
                        provider=provider,
                        interaction_id=waiter.interaction.interaction_id,
                        kind="approval",
                        options=options,
                    ),
                )
                sent_message_id = getattr(sent, "message_id", None)
                if isinstance(sent_message_id, int):
                    await self.interactions.bind_message_id(
                        user_id=user_id,
                        provider=provider,
                        interaction_id=waiter.interaction.interaction_id,
                        message_id=sent_message_id,
                    )
            except Exception:
                await self.interactions.discard_interaction(user_id, provider, waiter.interaction.interaction_id)
                await self.state.record_interaction_result(waiter.interaction.interaction_id, "send_failed")
                raise

            decision: str = "decline"
            cancelled = False
            try:
                try:
                    decision = await self.interactions.wait_for_interaction(waiter, timeout_sec=self.interaction_timeout_sec)
                except TimeoutError:
                    status_text = "已超时"
                    decision = "cancel"
                    await self.state.record_interaction_result(waiter.interaction.interaction_id, "timed_out")
                    await self.api.send_message(chat_id, "确认已超时，本轮执行已取消。", reply_to=reply_to)
            except asyncio.CancelledError:
                cancelled = True
                await self.state.record_interaction_result(waiter.interaction.interaction_id, "cancelled")
                raise
            finally:
                if not cancelled:
                    if decision == "accept":
                        status_text = "已批准一次"
                    elif decision == "acceptForSession":
                        status_text = "本会话已放行"
                    elif decision == "decline":
                        status_text = "已拒绝"
                    elif decision == "cancel" and status_text != "已超时":
                        status_text = "已取消"
                    interaction_status = {
                        "accept": "accepted",
                        "acceptForSession": "accepted_for_session",
                        "decline": "declined",
                        "cancel": "cancelled" if status_text != "已超时" else "timed_out",
                    }.get(decision, "declined")
                    await self.state.record_interaction_result(waiter.interaction.interaction_id, interaction_status)
                await self._close_interaction_message(waiter.interaction, status=status_text)

            if decision == "accept":
                return decision
            if decision == "acceptForSession":
                return decision
            if decision == "decline":
                return decision
            if decision == "cancel":
                return decision
            return "decline"
        finally:
            if typing is not None:
                typing.resume()
            if orchestrator is not None:
                orchestrator.resume_after_interaction()

    async def _request_question(
        self,
        *,
        chat_id: int,
        reply_to: int,
        user_id: int,
        provider: AgentProvider,
        chat_type: str,
        request: QuestionRequest,
        orchestrator: Optional[StreamOrchestrator] = None,
        typing: Optional[TypingStatus] = None,
    ) -> Optional[list[str]]:
        if chat_type != "private":
            await self.api.send_message(
                chat_id,
                "当前执行需要你补充信息，但群聊/频道里暂不支持这类交互。请在私聊中重试。",
                reply_to=reply_to,
            )
            return None

        options = request.options
        reply_mode = request.reply_mode
        if reply_mode == "buttons" and not options:
            reply_mode = "text"
        status_text = "已结束"
        try:
            if orchestrator is not None:
                orchestrator.pause_for_interaction()
            if typing is not None:
                typing.pause()
            try:
                waiter = await self.interactions.open_interaction(
                    user_id=user_id,
                    provider=provider,
                    kind="question",
                    title=request.title,
                    body=request.body,
                    options=options,
                    reply_mode=reply_mode,
                    chat_id=chat_id,
                    message_id=None,
                    timeout_sec=self.interaction_timeout_sec,
                )
            except RuntimeError as exc:
                log(
                    "[warn] question interaction ignored because active run is missing "
                    f"(provider={provider}, user_id={user_id}, error={exc!r})"
                )
                return None

            try:
                note = "请在 10 分钟内回复，或发送 /cancel。"
                if reply_mode == "text":
                    note = "请直接发送下一条文字消息作为答案，或发送 /cancel。"
                sent = await self.api.send_message_with_result(
                    chat_id=chat_id,
                    text=self._compose_interaction_text(
                        request.title,
                        request.body,
                        note=note,
                        options=options,
                    ),
                    reply_to=reply_to,
                    reply_markup=(
                        self._interaction_markup(
                            provider=provider,
                            interaction_id=waiter.interaction.interaction_id,
                            kind="question",
                            options=options,
                        )
                        if reply_mode == "buttons"
                        else None
                    ),
                )
                sent_message_id = getattr(sent, "message_id", None)
                if isinstance(sent_message_id, int):
                    await self.interactions.bind_message_id(
                        user_id=user_id,
                        provider=provider,
                        interaction_id=waiter.interaction.interaction_id,
                        message_id=sent_message_id,
                    )
            except Exception:
                await self.interactions.discard_interaction(user_id, provider, waiter.interaction.interaction_id)
                await self.state.record_interaction_result(waiter.interaction.interaction_id, "send_failed")
                raise

            answer: Optional[str] = None
            cancelled = False
            try:
                try:
                    answer = await self.interactions.wait_for_interaction(waiter, timeout_sec=self.interaction_timeout_sec)
                except TimeoutError:
                    status_text = "已超时"
                    await self.state.record_interaction_result(waiter.interaction.interaction_id, "timed_out")
                    await self.api.send_message(chat_id, "问题已超时，本轮执行已取消。", reply_to=reply_to)
                    return None
            except asyncio.CancelledError:
                cancelled = True
                await self.state.record_interaction_result(waiter.interaction.interaction_id, "cancelled")
                raise
            finally:
                if not cancelled:
                    if answer == "cancel" and status_text != "已超时":
                        status_text = "已取消"
                    elif isinstance(answer, str) and answer.strip():
                        status_text = f"已回答: {answer.strip()}"
                        if reply_mode == "buttons":
                            for option in options:
                                if option.id == answer:
                                    status_text = f"已回答: {option.label}"
                                    break
                    interaction_status = "answered"
                    if answer == "cancel":
                        interaction_status = "cancelled" if status_text != "已超时" else "timed_out"
                    await self.state.record_interaction_result(waiter.interaction.interaction_id, interaction_status)
                await self._close_interaction_message(waiter.interaction, status=status_text)

            if answer == "cancel":
                return None
            if isinstance(answer, str) and answer.strip():
                if reply_mode == "buttons":
                    for option in options:
                        if option.id == answer:
                            return [option.label]
                return [answer.strip()]
            return None
        finally:
            if typing is not None:
                typing.resume()
            if orchestrator is not None:
                orchestrator.resume_after_interaction()

    async def _try_handle_pending_image_prompt(
        self,
        chat_id: int,
        reply_to: int,
        user_id: int,
        provider: AgentProvider,
        text: str,
        chat_type: str,
    ) -> bool:
        pending = await self.state.get_pending_image(user_id, provider=provider)
        if pending is None:
            return False

        if not pending.path.is_file():
            await self.state.clear_pending_image(user_id, provider=provider)
            await self.api.send_message(
                chat_id,
                "之前缓存的图片已失效，请重新发送图片并附上说明。",
                reply_to=reply_to,
            )
            return True

        await self.state.clear_pending_image(user_id, provider=provider)
        await self._run_prompt(
            chat_id=chat_id,
            reply_to=reply_to,
            user_id=user_id,
            provider=provider,
            prompt=text,
            chat_type=chat_type,
            images=(pending.to_prompt_image(),),
        )
        return True

    async def _handle_image_message(
        self,
        chat_id: int,
        reply_to: int,
        user_id: int,
        provider: AgentProvider,
        image: IncomingImage,
        caption: str,
        media_group_id: Optional[str],
        chat_type: str,
    ) -> None:
        if media_group_id:
            await self.api.send_message(
                chat_id,
                "暂不支持相册或媒体组，请单独发送一张图片。",
                reply_to=reply_to,
            )
            return

        if image.file_size is not None and image.file_size > MAX_TELEGRAM_IMAGE_BYTES:
            await self.api.send_message(
                chat_id,
                "图片过大，当前仅支持不超过 20 MB 的单张图片。",
                reply_to=reply_to,
            )
            return

        try:
            downloaded_image = await self._download_prompt_image(chat_id, user_id, reply_to, image)
        except Exception as exc:
            log(
                "[error] telegram image download failed "
                f"(user_id={user_id}, provider={provider}, message_id={reply_to}, error={exc!r})"
            )
            await self.api.send_message(chat_id, f"图片下载失败: {exc}", reply_to=reply_to)
            return
        previous = await self._clear_pending_image_and_files(user_id, provider)
        if previous is not None:
            log(
                "replaced pending image "
                f"(user_id={user_id}, provider={provider}, old_message_id={previous.message_id})"
            )
        raw_caption = caption.strip()
        prompt = raw_caption
        if raw_caption == "/ask" or raw_caption.startswith("/ask "):
            ask_prompt = raw_caption[4:].strip()
            if not ask_prompt:
                self._delete_attachment_dir(downloaded_image.path.parent)
                await self.api.send_message(chat_id, "请在 /ask 后补充你的要求。", reply_to=reply_to)
                return
            prompt = ask_prompt

        if prompt:
            await self._run_prompt(
                chat_id=chat_id,
                reply_to=reply_to,
                user_id=user_id,
                provider=provider,
                prompt=prompt,
                chat_type=chat_type,
                images=(downloaded_image.to_prompt_image(),),
            )
            return

        await self.state.set_pending_image(user_id, downloaded_image, provider=provider)
        await self.api.send_message(
            chat_id,
            "已收到这张图片。请下一条发送文本，说明你希望我基于这张图做什么。",
            reply_to=reply_to,
        )

    async def _send_profiled_message(
        self,
        chat_id: int,
        text: str,
        profile: RenderProfile,
        reply_to: Optional[int] = None,
        reply_markup: Optional[InlineKeyboardMarkup] = None,
    ) -> None:
        if self.renderer is None:
            await self.api.send_message(chat_id, text, reply_to=reply_to, reply_markup=reply_markup)
            return

        render_result = await self.renderer.render_text(text, profile)
        stats = await send_render_result(
            api=self.api,
            chat_id=chat_id,
            render_result=render_result,
            reply_to=reply_to,
            reply_markup=reply_markup,
            fail_open=self.renderer.fail_open,
            log_prefix=f"rendered message fallback: profile={profile.value}",
        )
        log(
            "render summary: "
            f"profile={profile.value} mode={render_result.render_mode} "
            f"chunks={stats.total_items} fallback_chunks={stats.fallback_items} "
            f"parse_errors={render_result.parse_errors}"
        )

    async def _send_help(self, chat_id: int, reply_to: int) -> None:
        content = "\n".join(
            [
                "# 可用命令",
                "",
                "## Provider 与会话",
                "- `/provider [codex|claude]` 查看或切换 provider",
                "- `/sessions [N]` 查看当前 provider 最近 N 条会话",
                "- `/use <编号|session_id>` 切换当前 provider 会话",
                "- `/history [编号|session_id] [N]` 查看会话最近 N 条消息",
                "- `/new [cwd]` 进入新会话模式",
                "- `/status` 查看当前 provider 与会话状态",
                "- `/ask <内容>` 在当前 provider 手动提问",
                "- `/cancel` 取消当前执行或挂起中的确认",
                "",
                "> 执行 `/sessions` 后，可直接发送编号或点击按钮切换会话。",
                "> 直接发送普通消息即可续聊当前 session。",
                "> 单独发送一张图片并写 caption，或先发图片再下一条补文本要求。",
            ]
        )
        await self._send_profiled_message(
            chat_id=chat_id,
            text=content,
            profile=RenderProfile.COMMAND_HELP,
            reply_to=reply_to,
        )

    async def _handle_provider(self, chat_id: int, reply_to: int, user_id: int, arg: str) -> None:
        target = arg.strip().lower()
        current = await self.state.get_active_provider(user_id)
        if not target:
            lines = [
                f"当前 provider: {current}",
                "可选 provider:",
            ]
            for provider in ("codex", "claude"):
                runner_bin = self.runner_bins.get(provider, provider)
                status = "可用" if self._is_runner_available(runner_bin) else "不可用"
                lines.append(f"- {provider}: {status} ({runner_bin})")
            lines.append("切换示例: /provider claude")
            await self.api.send_message(chat_id, "\n".join(lines), reply_to=reply_to)
            return

        if target not in ("codex", "claude"):
            await self.api.send_message(chat_id, "参数错误，示例: /provider codex 或 /provider claude", reply_to=reply_to)
            return

        selected_provider = target
        if selected_provider == current:
            await self.api.send_message(chat_id, f"当前已是 {selected_provider}。", reply_to=reply_to)
            return

        await self.state.set_active_provider(user_id, selected_provider)
        await self.state.set_pending_session_pick(user_id, False, provider=selected_provider)
        await self.api.send_message(chat_id, f"已切换 provider: {selected_provider}", reply_to=reply_to)

    @staticmethod
    def _is_runner_available(runner_bin: str) -> bool:
        if "/" in runner_bin:
            path = Path(runner_bin).expanduser()
            return path.is_file() and os.access(path, os.X_OK)
        return shutil.which(runner_bin) is not None

    async def _handle_sessions(
        self,
        chat_id: int,
        reply_to: int,
        user_id: int,
        provider: AgentProvider,
        arg: str,
    ) -> None:
        limit = 10
        if arg:
            try:
                limit = max(1, min(30, int(arg)))
            except ValueError:
                await self.api.send_message(chat_id, "参数错误，示例: /sessions 10", reply_to=reply_to)
                return

        store = self.session_stores[provider]
        await store.refresh_recent()
        items = await store.list_recent(limit=limit)
        if not items:
            await self.api.send_message(chat_id, f"未找到 {provider} 本地会话记录。", reply_to=reply_to)
            return

        lines = [
            f"# 最近会话",
            f"provider: `{provider}`",
            "用 `/use 编号` 切换，或点击下方按钮。",
            "",
        ]
        session_ids = [s.session_id for s in items]
        keyboard_rows: list[list[InlineKeyboardButton]] = []
        for i, session in enumerate(items, start=1):
            short_id = session.session_id[:8]
            cwd_name = Path(session.cwd).name or session.cwd
            lines.append(f"{i}. **{session.title}**")
            lines.append(f"session: `{short_id}` | cwd: `{cwd_name}`")
            keyboard_rows.append(
                [InlineKeyboardButton(text=f"切换 {i}", callback_data=f"use:{provider}:{session.session_id}")]
            )

        lines.append("")
        lines.append("> 也可直接发送编号切换，例如发送 `1`。")
        markup = InlineKeyboardMarkup(inline_keyboard=keyboard_rows)
        await self._send_profiled_message(
            chat_id,
            "\n".join(lines),
            reply_to=reply_to,
            reply_markup=markup,
            profile=RenderProfile.SESSIONS,
        )

        await self.state.set_last_session_ids(user_id, session_ids, provider=provider)
        await self.state.set_pending_session_pick(user_id, True, provider=provider)

    async def _handle_use(
        self,
        chat_id: int,
        reply_to: int,
        user_id: int,
        provider: AgentProvider,
        arg: str,
    ) -> None:
        selector = arg.strip()
        if not selector:
            await self.api.send_message(chat_id, "示例: /use 1 或 /use <session_id>", reply_to=reply_to)
            return

        session_id, err = await self._resolve_session_selector(user_id, provider, selector)
        if err:
            await self.api.send_message(chat_id, err, reply_to=reply_to)
            return
        if not session_id:
            await self.api.send_message(chat_id, "无效的会话选择参数。", reply_to=reply_to)
            return

        await self._switch_to_session(chat_id, reply_to, user_id, provider, session_id)

    async def _switch_to_session(
        self,
        chat_id: int,
        reply_to: Optional[int],
        user_id: int,
        provider: AgentProvider,
        session_id: str,
    ) -> bool:
        store = self.session_stores[provider]
        await store.refresh_session(session_id)
        meta = await store.find_by_id(session_id)
        if not meta:
            await self.api.send_message(chat_id, f"未找到 {provider} session: {session_id}", reply_to=reply_to)
            return False

        await self.state.set_active_provider(user_id, provider)
        await self.state.set_active_session(user_id, meta.session_id, meta.cwd, provider=provider)
        await self.state.set_pending_session_pick(user_id, False, provider=provider)
        await self.api.send_message(
            chat_id,
            (
                f"已切换到 ({provider}):\n"
                f"{meta.title}\n"
                f"session: {meta.session_id}\n"
                f"cwd: {meta.cwd}\n"
                "现在可直接发消息对话。"
            ),
            reply_to=reply_to,
        )
        return True

    async def _try_handle_quick_session_pick(
        self,
        chat_id: int,
        reply_to: int,
        user_id: int,
        provider: AgentProvider,
        text: str,
    ) -> bool:
        if not await self.state.is_pending_session_pick(user_id, provider=provider):
            return False

        raw = text.strip()
        if not raw.isdigit():
            return False

        idx = int(raw)
        recent_ids = await self.state.get_last_session_ids(user_id, provider=provider)
        if idx <= 0 or idx > len(recent_ids):
            await self.api.send_message(chat_id, "编号无效。请发送 /sessions 重新查看列表。", reply_to=reply_to)
            return True

        await self._switch_to_session(chat_id, reply_to, user_id, provider, recent_ids[idx - 1])
        return True

    async def _handle_history(
        self,
        chat_id: int,
        reply_to: int,
        user_id: int,
        provider: AgentProvider,
        arg: str,
    ) -> None:
        tokens = [x for x in arg.split() if x]
        limit = 10
        session_id: Optional[str] = None

        if not tokens:
            session_id, _ = await self.state.get_active(user_id, provider=provider)
            if not session_id:
                await self.api.send_message(
                    chat_id,
                    "当前无 active session。先 /use 选择会话，或直接对话后再查看历史。",
                    reply_to=reply_to,
                )
                return
        else:
            session_id, err = await self._resolve_session_selector(user_id, provider, tokens[0])
            if err:
                await self.api.send_message(chat_id, err, reply_to=reply_to)
                return
            if not session_id:
                await self.api.send_message(chat_id, "无效的会话选择参数。", reply_to=reply_to)
                return
            if len(tokens) >= 2:
                try:
                    limit = int(tokens[1])
                except ValueError:
                    await self.api.send_message(chat_id, "N 必须是数字，示例: /history 1 20", reply_to=reply_to)
                    return

        limit = max(1, min(50, limit))
        store = self.session_stores[provider]
        await store.refresh_session(session_id)
        meta, messages = await store.get_history(session_id, limit=limit)
        if not meta:
            await self.api.send_message(chat_id, f"未找到 session: {session_id}", reply_to=reply_to)
            return
        if not messages:
            await self.api.send_message(chat_id, "该会话暂无可展示历史消息。", reply_to=reply_to)
            return

        lines = [
            "# 会话历史",
            f"provider: `{provider}`",
            f"title: {meta.title}",
            f"session: `{meta.session_id}`",
            f"显示最近 `{len(messages)}` 条消息：",
            "",
        ]
        for i, (role, message) in enumerate(messages, start=1):
            role_zh = "用户" if role == "user" else "助手"
            lines.append(f"{i}. **[{role_zh}]** {CodexSessionStore.compact_message(message)}")

        await self._send_profiled_message(
            chat_id=chat_id,
            text="\n".join(lines),
            profile=RenderProfile.HISTORY,
            reply_to=reply_to,
        )

    async def _resolve_session_selector(
        self,
        user_id: int,
        provider: AgentProvider,
        selector: str,
    ) -> tuple[Optional[str], Optional[str]]:
        raw = selector.strip()
        if not raw:
            return None, "示例: /use 1 或 /use <session_id>"
        if raw.isdigit():
            idx = int(raw)
            recent_ids = await self.state.get_last_session_ids(user_id, provider=provider)
            if idx <= 0 or idx > len(recent_ids):
                return None, "编号无效。先执行 /sessions，再用编号。"
            return recent_ids[idx - 1], None
        return raw, None

    async def _handle_status(
        self,
        chat_id: int,
        reply_to: int,
        user_id: int,
        provider: AgentProvider,
    ) -> None:
        session_id, cwd = await self.state.get_active(user_id, provider=provider)
        if not session_id:
            await self._send_profiled_message(
                chat_id,
                "\n".join(
                    [
                        "# 当前状态",
                        f"provider: `{provider}`",
                        "session: `未绑定`",
                        "提示: 先执行 `/sessions` + `/use`，或 `/new` 后直接发消息。",
                    ]
                ),
                reply_to=reply_to,
                profile=RenderProfile.COMMAND_STATUS,
            )
            return

        title = f"session {session_id[:8]}"
        store = self.session_stores[provider]
        await store.refresh_session(session_id)
        meta = await store.find_by_id(session_id)
        if meta:
            title = meta.title

        await self._send_profiled_message(
            chat_id,
            "\n".join(
                [
                    "# 当前状态",
                    f"provider: `{provider}`",
                    f"title: {title}",
                    f"session: `{session_id}`",
                    f"cwd: `{cwd or str(self.default_cwd)}`",
                    f"说明: 支持与本地 `{provider}` 客户端交替续聊。",
                ]
            ),
            reply_to=reply_to,
            profile=RenderProfile.COMMAND_STATUS,
        )

    async def _handle_ask(
        self,
        chat_id: int,
        reply_to: int,
        user_id: int,
        provider: AgentProvider,
        arg: str,
        chat_type: str,
    ) -> None:
        prompt = arg.strip()
        if not prompt:
            await self.api.send_message(chat_id, "示例: /ask 帮我总结当前仓库结构", reply_to=reply_to)
            return
        await self._run_prompt(chat_id, reply_to, user_id, provider, prompt, chat_type)

    async def _handle_new(
        self,
        chat_id: int,
        reply_to: int,
        user_id: int,
        provider: AgentProvider,
        arg: str,
    ) -> None:
        cwd_raw = arg.strip()
        _, current_cwd = await self.state.get_active(user_id, provider=provider)
        target_cwd = Path(current_cwd).expanduser() if current_cwd else self.default_cwd
        if cwd_raw:
            candidate = Path(cwd_raw).expanduser().resolve()
            if not candidate.exists() or not candidate.is_dir():
                await self.api.send_message(chat_id, f"cwd 不存在或不是目录: {candidate}", reply_to=reply_to)
                return
            if not self._is_allowed_cwd(candidate):
                await self.api.send_message(chat_id, f"cwd 不在允许范围内: {candidate}", reply_to=reply_to)
                return
            target_cwd = candidate
        elif not self._is_allowed_cwd(target_cwd):
            await self.api.send_message(chat_id, f"cwd 不在允许范围内: {target_cwd}", reply_to=reply_to)
            return

        await self.state.clear_active_session(user_id, str(target_cwd), provider=provider)
        await self._clear_pending_image_and_files(user_id, provider)
        await self.state.set_pending_session_pick(user_id, False, provider=provider)
        await self.api.send_message(
            chat_id,
            f"已进入新会话模式 ({provider})，cwd: {target_cwd}\n下一条普通消息会创建一个新 session。",
            reply_to=reply_to,
        )

    async def _handle_chat_message(
        self,
        chat_id: int,
        reply_to: int,
        user_id: int,
        provider: AgentProvider,
        text: str,
        chat_type: str,
    ) -> None:
        await self._run_prompt(chat_id, reply_to, user_id, provider, text, chat_type)

    async def _run_prompt(
        self,
        chat_id: int,
        reply_to: int,
        user_id: int,
        provider: AgentProvider,
        prompt: str,
        chat_type: str,
        images: tuple[PromptImage, ...] = (),
    ) -> None:
        active_run = await self.interactions.start_run(user_id, provider, chat_id, chat_type)
        if active_run is None:
            await self.api.send_message(chat_id, "当前 provider 已有执行在进行，请先等待完成或发送 /cancel。", reply_to=reply_to)
            for image in images:
                self._delete_attachment_dir(image.path.parent)
            return

        active_id: Optional[str] = None
        cwd = self.default_cwd
        final_status = "failed"
        final_answer = ""
        final_stderr = ""
        final_return_code = -1
        final_session_after: Optional[str] = None
        try:
            active_id, active_cwd = await self.state.get_active(user_id, provider=provider)
            final_session_after = active_id
            resolved_cwd = Path(active_cwd).expanduser() if active_cwd else self.default_cwd
            cwd = resolved_cwd
            if not resolved_cwd.exists() or not resolved_cwd.is_dir():
                fallback = Path.cwd()
                log(
                    "[warn] invalid run cwd, fallback to process cwd "
                    f"(provider={provider}, user_id={user_id}, resolved={resolved_cwd}, fallback={fallback})"
                )
                cwd = fallback
            cwd = cwd.resolve()
            if not self._is_allowed_cwd(cwd):
                final_status = "invalid_cwd"
                final_answer = f"cwd 不在允许范围内: {cwd}"
                final_return_code = 126
                await self.api.send_message(chat_id, f"cwd 不在允许范围内: {cwd}", reply_to=reply_to)
                return

            mode = "继续当前会话" if active_id else "新建会话"
            log(
                "run prompt: "
                f"user_id={user_id} provider={provider} mode={mode} cwd={cwd} "
                f"session={active_id} image_count={len(images)}"
            )

            use_stream = self.stream_config.enabled and chat_type == "private"
            orchestrator = StreamOrchestrator(
                api=self.api,
                chat_id=chat_id,
                reply_to=reply_to,
                stream_enabled=use_stream,
                stream_config=self.stream_config,
                renderer=self.renderer,
            )
            await orchestrator.start()

            typing = TypingStatus(self.api, chat_id)
            typing.start()
            runner = self.runners[provider]
            provider_label = provider.capitalize()
            bridge = RunInteractionBridge(
                service=self,
                chat_id=chat_id,
                reply_to=reply_to,
                user_id=user_id,
                provider=provider,
                chat_type=chat_type,
                orchestrator=orchestrator,
                typing=typing,
            )
            try:
                current_task = asyncio.current_task()
                if current_task is not None:
                    await self.interactions.set_task(user_id, provider, current_task)
                result = await runner.run_prompt(
                    prompt=prompt,
                    cwd=cwd,
                    session_id=active_id,
                    images=images,
                    on_partial=orchestrator.on_partial if use_stream else None,
                    on_reasoning=orchestrator.on_reasoning if use_stream else None,
                    interaction_handler=bridge,
                    cancel_event=active_run.cancel_event,
                )
            except Exception as exc:
                err_msg = f"调用 {provider_label} 时出现异常: {exc}"
                final_status = "exception"
                final_answer = err_msg
                final_stderr = str(exc)
                final_return_code = 1
                log(
                    "[error] provider runner exception "
                    f"(provider={provider}, user_id={user_id}, cwd={cwd}, session={active_id}, error={exc!r})"
                )
                await orchestrator.finalize_error(err_msg, reply_to=reply_to)
                log(orchestrator.summary_line(exit_code=-1))
                return
            finally:
                await typing.stop()
                await orchestrator.stop()

            if result.thread_id:
                await self.state.set_active_session(user_id, result.thread_id, str(cwd), provider=provider)
                final_session_after = result.thread_id
            else:
                final_session_after = active_id

            refresh_target = result.thread_id or active_id
            if refresh_target:
                try:
                    await self.session_stores[provider].refresh_session(refresh_target)
                except Exception as exc:
                    log(
                        "[warn] session refresh after run failed "
                        f"(provider={provider}, session={refresh_target}, error={exc!r})"
                    )

            if result.return_code != 0:
                msg = f"{provider_label} 执行失败 (exit={result.return_code})\n{result.answer}"
                if result.stderr_text:
                    msg += f"\n\nstderr:\n{result.stderr_text[-1200:]}"
                final_status = "cancelled" if result.return_code == 130 else "failed"
                final_answer = result.answer
                final_stderr = result.stderr_text
                final_return_code = result.return_code
                log(
                    "[error] provider runner non-zero exit "
                    f"(provider={provider}, user_id={user_id}, cwd={cwd}, session={active_id}, "
                    f"exit={result.return_code}, answer_{self._text_fingerprint(result.answer)}, "
                    f"stderr_{self._text_fingerprint(result.stderr_text)})"
                )
                await orchestrator.finalize_error(msg, reply_to=reply_to)
                log(orchestrator.summary_line(exit_code=result.return_code))
                return

            final_status = "succeeded"
            final_answer = result.answer
            final_stderr = result.stderr_text
            final_return_code = result.return_code
            await orchestrator.finalize_success(result.answer, reply_to=reply_to)
            log(orchestrator.summary_line(exit_code=result.return_code))
        finally:
            await self.state.record_run_result(
                user_id=user_id,
                provider=provider,
                run_id=active_run.run_id,
                status=final_status,
                cwd=cwd,
                session_id_before=active_id,
                session_id_after=final_session_after,
                prompt=prompt,
                answer=final_answer,
                stderr_text=final_stderr,
                return_code=final_return_code,
                attachment_ref_ids=tuple(
                    image.attachment_ref_id
                    for image in images
                    if isinstance(image.attachment_ref_id, int)
                ),
            )
            await self.interactions.finish_run(user_id, provider, active_run.run_id)
            for image in images:
                self._delete_attachment_dir(image.path.parent)

    def _is_allowed_cwd(self, candidate: Path) -> bool:
        if not self.allowed_cwd_roots:
            return True
        resolved = candidate.expanduser().resolve()
        return any(resolved.is_relative_to(root) for root in self.allowed_cwd_roots)


def build_router(service: TgCodexService) -> Router:
    router = Router(name="tg_codex")

    @router.callback_query()
    async def _callback_handler(callback_query: CallbackQuery) -> None:
        await service.handle_callback_query(callback_query)

    @router.message()
    async def _message_handler(message: Message) -> None:
        await service.handle_message(message)

    return router
