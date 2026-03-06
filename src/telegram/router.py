import os
import shutil
from pathlib import Path
from typing import Optional

from aiogram import Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from ..domain.models import AgentProvider, StreamConfig
from ..logging_utils import log
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
]


class TgCodexService:
    def __init__(
        self,
        api: TelegramClient,
        session_stores: dict[AgentProvider, AsyncSessionStoreProtocol],
        state: StateStore,
        runners: dict[AgentProvider, RunnerProtocol],
        runner_bins: dict[AgentProvider, str],
        default_cwd: Path,
        allowed_user_ids: Optional[set[int]],
        allowed_cwd_roots: tuple[Path, ...],
        stream_config: StreamConfig,
        renderer: Optional[TelegramMessageRenderer] = None,
    ):
        self.api = api
        self.session_stores = session_stores
        self.state = state
        self.runners = runners
        self.runner_bins = runner_bins
        self.default_cwd = default_cwd
        self.allowed_user_ids = allowed_user_ids
        self.allowed_cwd_roots = tuple(path.expanduser().resolve() for path in allowed_cwd_roots)
        self.stream_config = stream_config
        self.renderer = renderer

    async def shutdown(self) -> None:
        await self.state.close()

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
        reply_to = getattr(msg, "message_id", None)
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
            await self._switch_to_session(chat_id, reply_to, int(user_id), provider, session_id)
            return

        await self.api.answer_callback_query(cq_id, text="不支持的操作。", show_alert=True)

    async def handle_message(self, msg: Message) -> None:
        text = (msg.text or "").strip()
        if not text:
            return

        chat = msg.chat
        chat_id = chat.id
        chat_type = str(chat.type)
        message_id = msg.message_id
        user = msg.from_user
        user_id = getattr(user, "id", None)

        if user_id is None:
            return

        log(f"update received: user_id={user_id} chat_id={chat_id} text={text[:80]!r}")
        if self.allowed_user_ids is not None and int(user_id) not in self.allowed_user_ids:
            log(f"blocked by allowlist: user_id={user_id}")
            await self.api.send_message(chat_id, "没有权限使用这个 bot。", reply_to=message_id)
            return

        provider = await self.state.get_active_provider(int(user_id))
        if not text.startswith("/"):
            if await self._try_handle_quick_session_pick(chat_id, message_id, int(user_id), provider, text):
                return
            await self.state.set_pending_session_pick(int(user_id), False, provider=provider)
            await self._handle_chat_message(chat_id, message_id, int(user_id), provider, text, chat_type)
            return

        cmd, arg = self._parse_command(text)
        log(f"command: /{cmd} arg={arg[:80]!r}")

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
        if cmd == "new":
            await self._handle_new(chat_id, message_id, int(user_id), provider, arg)
            return
        if cmd == "history":
            await self._handle_history(chat_id, message_id, int(user_id), provider, arg)
            return
        if cmd == "ask":
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
                "",
                "> 执行 `/sessions` 后，可直接发送编号或点击按钮切换会话。",
                "> 直接发送普通消息即可续聊当前 session。",
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
    ) -> None:
        store = self.session_stores[provider]
        meta = await store.find_by_id(session_id)
        if not meta:
            await self.api.send_message(chat_id, f"未找到 {provider} session: {session_id}", reply_to=reply_to)
            return

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
    ) -> None:
        active_id, active_cwd = await self.state.get_active(user_id, provider=provider)
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
            await self.api.send_message(chat_id, f"cwd 不在允许范围内: {cwd}", reply_to=reply_to)
            return

        mode = "继续当前会话" if active_id else "新建会话"
        log(f"run prompt: user_id={user_id} provider={provider} mode={mode} cwd={cwd} session={active_id}")

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
        try:
            result = await runner.run_prompt(
                prompt=prompt,
                cwd=cwd,
                session_id=active_id,
                on_partial=orchestrator.on_partial if use_stream else None,
                on_reasoning=orchestrator.on_reasoning if use_stream else None,
            )
        except Exception as exc:
            err_msg = f"调用 {provider_label} 时出现异常: {exc}"
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

        if result.return_code != 0:
            msg = f"{provider_label} 执行失败 (exit={result.return_code})\n{result.answer}"
            if result.stderr_text:
                msg += f"\n\nstderr:\n{result.stderr_text[-1200:]}"
            log(
                "[error] provider runner non-zero exit "
                f"(provider={provider}, user_id={user_id}, cwd={cwd}, session={active_id}, "
                f"exit={result.return_code}, answer_tail={result.answer[-400:]!r}, "
                f"stderr_tail={result.stderr_text[-400:]!r})"
            )
            await orchestrator.finalize_error(msg, reply_to=reply_to)
            log(orchestrator.summary_line(exit_code=result.return_code))
            return

        await orchestrator.finalize_success(result.answer, reply_to=reply_to)
        log(orchestrator.summary_line(exit_code=result.return_code))

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
