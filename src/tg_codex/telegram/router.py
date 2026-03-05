from pathlib import Path
from typing import Optional

from aiogram import Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from tg_codex.logging_utils import log
from tg_codex.services.codex_runner import CodexRunner
from tg_codex.services.session_store import SessionStore
from tg_codex.services.state_store import StateStore
from tg_codex.telegram.client import TelegramClient
from tg_codex.telegram.streaming import StreamOrchestrator, TypingStatus

BOT_COMMANDS: list[dict[str, str]] = [
    {"command": "start", "description": "开始使用"},
    {"command": "help", "description": "查看帮助"},
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
        sessions: SessionStore,
        state: StateStore,
        codex: CodexRunner,
        default_cwd: Path,
        allowed_user_ids: Optional[set[int]],
        stream_enabled: bool,
        stream_edit_interval_ms: int,
        stream_min_delta_chars: int,
        thinking_status_interval_ms: int,
    ):
        self.api = api
        self.sessions = sessions
        self.state = state
        self.codex = codex
        self.default_cwd = default_cwd
        self.allowed_user_ids = allowed_user_ids
        self.stream_enabled = stream_enabled
        self.stream_edit_interval_ms = max(200, stream_edit_interval_ms)
        self.stream_min_delta_chars = max(1, stream_min_delta_chars)
        self.thinking_status_interval_ms = max(400, thinking_status_interval_ms)

    async def setup_bot_menu(self) -> None:
        await self.api.set_my_commands(BOT_COMMANDS)
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
            session_id = data[4:]
            await self.api.answer_callback_query(cq_id, text="正在切换会话...")
            await self._switch_to_session(chat_id, reply_to, int(user_id), session_id)
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

        if not text.startswith("/"):
            if await self._try_handle_quick_session_pick(chat_id, message_id, int(user_id), text):
                return
            self.state.set_pending_session_pick(int(user_id), False)
            await self._handle_chat_message(chat_id, message_id, int(user_id), text, chat_type)
            return

        cmd, arg = self._parse_command(text)
        log(f"command: /{cmd} arg={arg[:80]!r}")

        if cmd in ("start", "help"):
            await self._send_help(chat_id, message_id)
            return
        if cmd == "sessions":
            await self._handle_sessions(chat_id, message_id, arg, int(user_id))
            return
        if cmd == "use":
            await self._handle_use(chat_id, message_id, int(user_id), arg)
            return
        if cmd == "status":
            await self._handle_status(chat_id, message_id, int(user_id))
            return
        if cmd == "new":
            await self._handle_new(chat_id, message_id, int(user_id), arg)
            return
        if cmd == "history":
            await self._handle_history(chat_id, message_id, int(user_id), arg)
            return
        if cmd == "ask":
            await self._handle_ask(chat_id, message_id, int(user_id), arg, chat_type)
            return

        await self.api.send_message(chat_id, f"未知命令: /{cmd}\n发送 /help 查看说明。", reply_to=message_id)

    @staticmethod
    def _parse_command(text: str) -> tuple[str, str]:
        parts = text.split(" ", 1)
        cmd = parts[0][1:]
        cmd = cmd.split("@", 1)[0].strip().lower()
        arg = parts[1].strip() if len(parts) > 1 else ""
        return cmd, arg

    async def _send_help(self, chat_id: int, reply_to: int) -> None:
        await self.api.send_message(
            chat_id,
            "\n".join(
                [
                    "可用命令:",
                    "/sessions [N] - 查看最近 N 条会话（标题 + 编号）",
                    "/use <编号|session_id> - 切换当前会话",
                    "/history [编号|session_id] [N] - 查看会话最近 N 条消息",
                    "/new [cwd] - 进入新会话模式（下一条普通消息会新建 session）",
                    "/status - 查看当前绑定会话",
                    "/ask <内容> - 手动提问（可选）",
                    "执行 /sessions 后，可直接发送编号切换会话",
                    "执行 /sessions 后，也可点击按钮直接切换会话",
                    "直接发普通消息即可对话（会自动续聊当前 session）",
                ]
            ),
            reply_to=reply_to,
        )

    async def _handle_sessions(self, chat_id: int, reply_to: int, arg: str, user_id: int) -> None:
        limit = 10
        if arg:
            try:
                limit = max(1, min(30, int(arg)))
            except ValueError:
                await self.api.send_message(chat_id, "参数错误，示例: /sessions 10", reply_to=reply_to)
                return

        items = self.sessions.list_recent(limit=limit)
        if not items:
            await self.api.send_message(chat_id, "未找到本地会话记录。", reply_to=reply_to)
            return

        lines = ["最近会话（用 /use 编号 切换）:"]
        session_ids = [s.session_id for s in items]
        keyboard_rows: list[list[InlineKeyboardButton]] = []
        for i, session in enumerate(items, start=1):
            short_id = session.session_id[:8]
            cwd_name = Path(session.cwd).name or session.cwd
            lines.append(f"{i}. {session.title} | {short_id} | {cwd_name}")
            keyboard_rows.append([InlineKeyboardButton(text=f"切换 {i}", callback_data=f"use:{session.session_id}")])

        lines.append("直接发送编号即可切换（例如发送: 1）")
        markup = InlineKeyboardMarkup(inline_keyboard=keyboard_rows)
        await self.api.send_message(
            chat_id,
            "\n".join(lines),
            reply_to=reply_to,
            reply_markup=markup,
        )

        self.state.set_last_session_ids(user_id, session_ids)
        self.state.set_pending_session_pick(user_id, True)

    async def _handle_use(self, chat_id: int, reply_to: int, user_id: int, arg: str) -> None:
        selector = arg.strip()
        if not selector:
            await self.api.send_message(chat_id, "示例: /use 1 或 /use <session_id>", reply_to=reply_to)
            return

        session_id, err = self._resolve_session_selector(user_id, selector)
        if err:
            await self.api.send_message(chat_id, err, reply_to=reply_to)
            return
        if not session_id:
            await self.api.send_message(chat_id, "无效的会话选择参数。", reply_to=reply_to)
            return

        await self._switch_to_session(chat_id, reply_to, user_id, session_id)

    async def _switch_to_session(
        self,
        chat_id: int,
        reply_to: Optional[int],
        user_id: int,
        session_id: str,
    ) -> None:
        meta = self.sessions.find_by_id(session_id)
        if not meta:
            await self.api.send_message(chat_id, f"未找到 session: {session_id}", reply_to=reply_to)
            return

        self.state.set_active_session(user_id, meta.session_id, meta.cwd)
        self.state.set_pending_session_pick(user_id, False)
        await self.api.send_message(
            chat_id,
            f"已切换到:\n{meta.title}\nsession: {meta.session_id}\ncwd: {meta.cwd}\n现在可直接发消息对话。",
            reply_to=reply_to,
        )

    async def _try_handle_quick_session_pick(self, chat_id: int, reply_to: int, user_id: int, text: str) -> bool:
        if not self.state.is_pending_session_pick(user_id):
            return False

        raw = text.strip()
        if not raw.isdigit():
            return False

        idx = int(raw)
        recent_ids = self.state.get_last_session_ids(user_id)
        if idx <= 0 or idx > len(recent_ids):
            await self.api.send_message(chat_id, "编号无效。请发送 /sessions 重新查看列表。", reply_to=reply_to)
            return True

        await self._switch_to_session(chat_id, reply_to, user_id, recent_ids[idx - 1])
        return True

    async def _handle_history(self, chat_id: int, reply_to: int, user_id: int, arg: str) -> None:
        tokens = [x for x in arg.split() if x]
        limit = 10
        session_id: Optional[str] = None

        if not tokens:
            session_id, _ = self.state.get_active(user_id)
            if not session_id:
                await self.api.send_message(
                    chat_id,
                    "当前无 active session。先 /use 选择会话，或直接对话后再查看历史。",
                    reply_to=reply_to,
                )
                return
        else:
            session_id, err = self._resolve_session_selector(user_id, tokens[0])
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
        meta, messages = self.sessions.get_history(session_id, limit=limit)
        if not meta:
            await self.api.send_message(chat_id, f"未找到 session: {session_id}", reply_to=reply_to)
            return
        if not messages:
            await self.api.send_message(chat_id, "该会话暂无可展示历史消息。", reply_to=reply_to)
            return

        lines = [
            f"会话历史: {meta.title}",
            f"session: {meta.session_id}",
            f"显示最近 {len(messages)} 条消息:",
        ]
        for i, (role, message) in enumerate(messages, start=1):
            role_zh = "用户" if role == "user" else "助手"
            lines.append(f"{i}. [{role_zh}] {SessionStore.compact_message(message)}")

        await self.api.send_message(chat_id, "\n".join(lines), reply_to=reply_to)

    def _resolve_session_selector(self, user_id: int, selector: str) -> tuple[Optional[str], Optional[str]]:
        raw = selector.strip()
        if not raw:
            return None, "示例: /use 1 或 /use <session_id>"
        if raw.isdigit():
            idx = int(raw)
            recent_ids = self.state.get_last_session_ids(user_id)
            if idx <= 0 or idx > len(recent_ids):
                return None, "编号无效。先执行 /sessions，再用编号。"
            return recent_ids[idx - 1], None
        return raw, None

    async def _handle_status(self, chat_id: int, reply_to: int, user_id: int) -> None:
        session_id, cwd = self.state.get_active(user_id)
        if not session_id:
            await self.api.send_message(
                chat_id,
                "当前没有绑定会话。可先 /sessions + /use，或 /new 后直接发消息。",
                reply_to=reply_to,
            )
            return

        title = f"session {session_id[:8]}"
        meta = self.sessions.find_by_id(session_id)
        if meta:
            title = meta.title

        await self.api.send_message(
            chat_id,
            f"当前会话:\n{title}\nsession: {session_id}\ncwd: {cwd or str(self.default_cwd)}\n支持与本地 Codex 客户端交替续聊。",
            reply_to=reply_to,
        )

    async def _handle_ask(self, chat_id: int, reply_to: int, user_id: int, arg: str, chat_type: str) -> None:
        prompt = arg.strip()
        if not prompt:
            await self.api.send_message(chat_id, "示例: /ask 帮我总结当前仓库结构", reply_to=reply_to)
            return
        await self._run_prompt(chat_id, reply_to, user_id, prompt, chat_type)

    async def _handle_new(self, chat_id: int, reply_to: int, user_id: int, arg: str) -> None:
        cwd_raw = arg.strip()
        _, current_cwd = self.state.get_active(user_id)
        target_cwd = Path(current_cwd).expanduser() if current_cwd else self.default_cwd
        if cwd_raw:
            candidate = Path(cwd_raw).expanduser()
            if not candidate.exists() or not candidate.is_dir():
                await self.api.send_message(chat_id, f"cwd 不存在或不是目录: {candidate}", reply_to=reply_to)
                return
            target_cwd = candidate

        self.state.clear_active_session(user_id, str(target_cwd))
        self.state.set_pending_session_pick(user_id, False)
        await self.api.send_message(
            chat_id,
            f"已进入新会话模式，cwd: {target_cwd}\n下一条普通消息会创建一个新 session。",
            reply_to=reply_to,
        )

    async def _handle_chat_message(self, chat_id: int, reply_to: int, user_id: int, text: str, chat_type: str) -> None:
        await self._run_prompt(chat_id, reply_to, user_id, text, chat_type)

    async def _run_prompt(
        self,
        chat_id: int,
        reply_to: int,
        user_id: int,
        prompt: str,
        chat_type: str,
    ) -> None:
        active_id, active_cwd = self.state.get_active(user_id)
        cwd = Path(active_cwd).expanduser() if active_cwd else self.default_cwd
        if not cwd.exists():
            cwd = self.default_cwd

        mode = "继续当前会话" if active_id else "新建会话"
        log(f"run prompt: user_id={user_id} mode={mode} cwd={cwd} session={active_id}")

        use_stream = self.stream_enabled and chat_type == "private"
        orchestrator = StreamOrchestrator(
            api=self.api,
            chat_id=chat_id,
            reply_to=reply_to,
            stream_enabled=use_stream,
            stream_edit_interval_ms=self.stream_edit_interval_ms,
            stream_min_delta_chars=self.stream_min_delta_chars,
            thinking_status_interval_ms=self.thinking_status_interval_ms,
        )
        await orchestrator.start()

        typing = TypingStatus(self.api, chat_id)
        typing.start()
        try:
            result = await self.codex.run_prompt(
                prompt=prompt,
                cwd=cwd,
                session_id=active_id,
                on_partial=orchestrator.on_partial if use_stream else None,
                on_reasoning=orchestrator.on_reasoning if use_stream else None,
            )
        except Exception as exc:
            err_msg = f"调用 Codex 时出现异常: {exc}"
            await orchestrator.finalize_error(err_msg, reply_to=reply_to)
            log(orchestrator.summary_line(exit_code=-1))
            return
        finally:
            await typing.stop()
            await orchestrator.stop()

        if result.thread_id:
            self.state.set_active_session(user_id, result.thread_id, str(cwd))

        if result.return_code != 0:
            msg = f"Codex 执行失败 (exit={result.return_code})\n{result.answer}"
            if result.stderr_text:
                msg += f"\n\nstderr:\n{result.stderr_text[-1200:]}"
            await orchestrator.finalize_error(msg, reply_to=reply_to)
            log(orchestrator.summary_line(exit_code=result.return_code))
            return

        await orchestrator.finalize_success(result.answer, reply_to=reply_to)
        log(orchestrator.summary_line(exit_code=result.return_code))



def build_router(service: TgCodexService) -> Router:
    router = Router(name="tg_codex")

    @router.callback_query()
    async def _callback_handler(callback_query: CallbackQuery) -> None:
        await service.handle_callback_query(callback_query)

    @router.message()
    async def _message_handler(message: Message) -> None:
        await service.handle_message(message)

    return router
