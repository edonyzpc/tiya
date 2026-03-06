import asyncio
from collections import deque
from dataclasses import dataclass, field
import json
import re
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from ..domain.models import AgentRunResult, ApprovalRequest, InteractionOption, PromptImage, QuestionRequest
from ..logging_utils import log
from .runner_protocol import InteractionHandlerProtocol

MAX_CAPTURED_LINES = 200


class _JsonRpcError(RuntimeError):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class _TurnState:
    thread_id: Optional[str] = None
    turn_id: Optional[str] = None
    item_texts: dict[str, str] = field(default_factory=dict)
    item_order: list[str] = field(default_factory=list)
    reasoning_parts: dict[str, list[str]] = field(default_factory=dict)
    turn_status: Optional[str] = None
    turn_error: Optional[str] = None
    last_partial: str = ""
    last_reasoning: str = ""


class _AppServerClient:
    def __init__(self, codex_bin: str, cwd: Path, stdout_lines: deque[str], stderr_lines: deque[str]):
        self.codex_bin = codex_bin
        self.cwd = cwd
        self.stdout_lines = stdout_lines
        self.stderr_lines = stderr_lines
        self.proc: Optional[asyncio.subprocess.Process] = None
        self._reader_task: Optional[asyncio.Task[None]] = None
        self._stderr_task: Optional[asyncio.Task[None]] = None
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._request_id = 0
        self._write_lock = asyncio.Lock()
        self._notification_handler: Optional[Callable[[dict[str, Any]], Awaitable[None]]] = None
        self._request_handler: Optional[Callable[[dict[str, Any]], Awaitable[None]]] = None

    async def start(
        self,
        notification_handler: Callable[[dict[str, Any]], Awaitable[None]],
        request_handler: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> None:
        self._notification_handler = notification_handler
        self._request_handler = request_handler
        self.proc = await asyncio.create_subprocess_exec(
            self.codex_bin,
            "app-server",
            "--listen",
            "stdio://",
            cwd=str(self.cwd),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        self._reader_task = asyncio.create_task(self._read_stdout())
        self._stderr_task = asyncio.create_task(self._read_stderr())

    async def close(self) -> None:
        proc = self.proc
        if proc is None:
            return
        if proc.returncode is None:
            proc.kill()
        try:
            await proc.wait()
        except Exception:
            pass
        for task in (self._reader_task, self._stderr_task):
            if task is None:
                continue
            if not task.done():
                task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self._request_id += 1
        request_id = self._request_id
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        await self._send(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": method,
                "params": params,
            }
        )
        response = await future
        if "error" in response:
            error = response["error"]
            if isinstance(error, dict):
                code = int(error.get("code", -32000))
                message = str(error.get("message", "Codex app-server error"))
                raise _JsonRpcError(code, message)
            raise _JsonRpcError(-32000, str(error))
        result = response.get("result")
        if not isinstance(result, dict):
            return {}
        return result

    async def notify(self, method: str, params: Optional[dict[str, Any]] = None) -> None:
        payload: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        await self._send(payload)

    async def respond(self, request_id: Any, result: dict[str, Any]) -> None:
        await self._send({"jsonrpc": "2.0", "id": request_id, "result": result})

    async def respond_error(self, request_id: Any, code: int, message: str) -> None:
        await self._send({"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}})

    async def _send(self, payload: dict[str, Any]) -> None:
        if self.proc is None or self.proc.stdin is None:
            raise RuntimeError("codex app-server is not running")
        async with self._write_lock:
            self.proc.stdin.write((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
            await self.proc.stdin.drain()

    async def _read_stdout(self) -> None:
        assert self.proc is not None
        assert self.proc.stdout is not None
        try:
            while True:
                raw = await self.proc.stdout.readline()
                if not raw:
                    break
                line = raw.decode("utf-8", errors="replace").rstrip("\n")
                self.stdout_lines.append(line)
                stripped = line.strip()
                if not stripped or not stripped.startswith("{"):
                    continue
                try:
                    message = json.loads(stripped)
                except json.JSONDecodeError:
                    continue

                if "id" in message and ("result" in message or "error" in message):
                    request_id = int(message["id"])
                    future = self._pending.pop(request_id, None)
                    if future is not None and not future.done():
                        future.set_result(message)
                    continue

                if "method" in message and "id" in message:
                    if self._request_handler is None:
                        await self.respond_error(message["id"], -32601, "request handler is not configured")
                        continue
                    await self._request_handler(message)
                    continue

                if "method" in message and self._notification_handler is not None:
                    await self._notification_handler(message)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(exc)
        finally:
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(RuntimeError("codex app-server disconnected"))

    async def _read_stderr(self) -> None:
        assert self.proc is not None
        assert self.proc.stderr is not None
        while True:
            raw = await self.proc.stderr.readline()
            if not raw:
                return
            self.stderr_lines.append(raw.decode("utf-8", errors="replace").rstrip("\n"))


class CodexRunner:
    def __init__(
        self,
        codex_bin: str,
        sandbox_mode: Optional[str] = None,
        approval_policy: Optional[str] = None,
        dangerous_bypass_level: int = 0,
    ):
        self.codex_bin = codex_bin
        self.sandbox_mode = (sandbox_mode or "").strip() or None
        self.approval_policy = (approval_policy or "").strip() or None
        self.dangerous_bypass_level = max(0, min(2, int(dangerous_bypass_level)))

    def _format_spawn_error(self, exc: FileNotFoundError) -> str:
        filename = str(getattr(exc, "filename", "") or "").strip()
        bin_candidates = {
            self.codex_bin,
            str(Path(self.codex_bin).expanduser()),
            Path(self.codex_bin).name,
        }
        if filename and filename not in bin_candidates:
            return f"工作目录不存在或不可访问: {filename}"
        return f"找不到 codex 可执行文件: {self.codex_bin}"

    async def run_prompt(
        self,
        prompt: str,
        cwd: Path,
        session_id: Optional[str] = None,
        images: tuple[PromptImage, ...] = (),
        on_partial: Optional[Callable[[str], Awaitable[None]]] = None,
        on_reasoning: Optional[Callable[[str], Awaitable[None]]] = None,
        interaction_handler: Optional[InteractionHandlerProtocol] = None,
        cancel_event: Optional[asyncio.Event] = None,
    ) -> AgentRunResult:
        stdout_lines: deque[str] = deque(maxlen=MAX_CAPTURED_LINES)
        stderr_lines: deque[str] = deque(maxlen=MAX_CAPTURED_LINES)
        state = _TurnState()
        turn_done: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        client = _AppServerClient(self.codex_bin, cwd, stdout_lines, stderr_lines)

        async def _emit_partial() -> None:
            partial = self._compose_agent_text(state.item_texts, state.item_order)
            if partial and partial != state.last_partial:
                state.last_partial = partial
                if on_partial is not None:
                    try:
                        await on_partial(partial)
                    except Exception:
                        pass

        async def _emit_reasoning() -> None:
            text = self._compose_reasoning_text(state.reasoning_parts)
            summary = self._normalize_reasoning_text(text)
            if summary and summary != state.last_reasoning:
                state.last_reasoning = summary
                if on_reasoning is not None:
                    try:
                        await on_reasoning(summary)
                    except Exception:
                        pass

        async def _handle_notification(message: dict[str, Any]) -> None:
            method = str(message.get("method") or "")
            params = message.get("params")
            if not isinstance(params, dict):
                return

            if method == "thread/started":
                thread = params.get("thread")
                if isinstance(thread, dict):
                    thread_id = thread.get("id")
                    if isinstance(thread_id, str) and thread_id:
                        state.thread_id = thread_id
                return

            if method == "turn/started":
                turn = params.get("turn")
                if isinstance(turn, dict):
                    turn_id = turn.get("id")
                    if isinstance(turn_id, str) and turn_id:
                        state.turn_id = turn_id
                return

            if method == "item/started":
                item = params.get("item")
                if not isinstance(item, dict):
                    return
                item_type = item.get("type")
                item_id = item.get("id")
                if not isinstance(item_id, str) or not item_id:
                    return
                if item_type == "agentMessage":
                    if item_id not in state.item_texts:
                        state.item_texts[item_id] = str(item.get("text") or "")
                        state.item_order.append(item_id)
                elif item_type == "reasoning":
                    state.reasoning_parts.setdefault(item_id, [])
                return

            if method == "item/agentMessage/delta":
                item_id = params.get("itemId")
                delta = params.get("delta")
                if isinstance(item_id, str) and isinstance(delta, str):
                    if item_id not in state.item_texts:
                        state.item_texts[item_id] = ""
                        state.item_order.append(item_id)
                    state.item_texts[item_id] += delta
                    await _emit_partial()
                return

            if method == "item/reasoning/summaryTextDelta":
                item_id = params.get("itemId")
                summary_index = params.get("summaryIndex")
                delta = params.get("delta")
                if isinstance(item_id, str) and isinstance(summary_index, int) and isinstance(delta, str):
                    parts = state.reasoning_parts.setdefault(item_id, [])
                    while len(parts) <= summary_index:
                        parts.append("")
                    parts[summary_index] += delta
                    await _emit_reasoning()
                return

            if method == "item/completed":
                item = params.get("item")
                if not isinstance(item, dict):
                    return
                item_type = item.get("type")
                item_id = item.get("id")
                if not isinstance(item_id, str) or not item_id:
                    return
                if item_type == "agentMessage":
                    if item_id not in state.item_texts:
                        state.item_order.append(item_id)
                    state.item_texts[item_id] = str(item.get("text") or "")
                    await _emit_partial()
                    return
                if item_type == "reasoning":
                    summary = item.get("summary")
                    if isinstance(summary, list):
                        state.reasoning_parts[item_id] = [str(part) for part in summary if str(part).strip()]
                        await _emit_reasoning()
                    return

            if method == "turn/completed":
                turn = params.get("turn")
                if isinstance(turn, dict):
                    state.turn_status = str(turn.get("status") or "")
                    error = turn.get("error")
                    if isinstance(error, dict):
                        message_text = error.get("message")
                        additional = error.get("additionalDetails")
                        bits = [str(message_text or "").strip()]
                        if isinstance(additional, str) and additional.strip():
                            bits.append(additional.strip())
                        state.turn_error = "\n".join(bit for bit in bits if bit).strip() or None
                if not turn_done.done():
                    turn_done.set_result(None)
                return

            if method == "error":
                error_message = params.get("message")
                if isinstance(error_message, str) and error_message.strip():
                    stderr_lines.append(error_message.strip())

        async def _handle_request(message: dict[str, Any]) -> None:
            request_id = message.get("id")
            method = str(message.get("method") or "")
            params = message.get("params")
            if not isinstance(params, dict):
                await client.respond_error(request_id, -32602, "invalid request params")
                return

            if method == "item/commandExecution/requestApproval":
                available = params.get("availableDecisions")
                allow_accept_for_session = isinstance(available, list) and "acceptForSession" in available
                decision = "decline"
                if interaction_handler is not None:
                    decision = await interaction_handler.request_approval(
                        ApprovalRequest(
                            kind="command",
                            title="Codex 请求执行命令",
                            body=str(params.get("reason") or "模型请求执行命令。"),
                            command=self._string_or_none(params.get("command")),
                            cwd=self._string_or_none(params.get("cwd")),
                            allow_accept_for_session=allow_accept_for_session,
                        )
                    )
                if decision == "acceptForSession" and not allow_accept_for_session:
                    decision = "accept"
                await client.respond(request_id, {"decision": decision})
                return

            if method == "item/fileChange/requestApproval":
                allow_accept_for_session = bool(params.get("grantRoot"))
                decision = "decline"
                if interaction_handler is not None:
                    reason_bits = [str(params.get("reason") or "模型请求修改文件。").strip()]
                    grant_root = self._string_or_none(params.get("grantRoot"))
                    if grant_root:
                        reason_bits.append(f"授权根目录: {grant_root}")
                    decision = await interaction_handler.request_approval(
                        ApprovalRequest(
                            kind="file_change",
                            title="Codex 请求修改文件",
                            body="\n".join(bit for bit in reason_bits if bit),
                            cwd=grant_root,
                            allow_accept_for_session=allow_accept_for_session,
                        )
                    )
                await client.respond(request_id, {"decision": decision})
                return

            if method == "item/tool/requestUserInput":
                response = await self._request_user_input_response(params, interaction_handler)
                await client.respond(request_id, response)
                return

            await client.respond_error(request_id, -32601, f"unsupported request: {method}")

        try:
            await client.start(_handle_notification, _handle_request)
        except FileNotFoundError as exc:
            return AgentRunResult(
                thread_id=None,
                answer=self._format_spawn_error(exc),
                stderr_text=str(exc),
                return_code=127,
            )

        cancel_task: Optional[asyncio.Task[None]] = None
        try:
            await client.request(
                "initialize",
                {
                    "clientInfo": {"name": "tiya", "version": "0.1"},
                    "capabilities": {"experimentalApi": True},
                },
            )
            await client.notify("initialized")

            thread_result = await client.request(
                "thread/resume" if session_id else "thread/start",
                self._thread_params(cwd, session_id, interaction_handler is not None),
            )
            thread = thread_result.get("thread")
            if isinstance(thread, dict):
                thread_id = thread.get("id")
                if isinstance(thread_id, str) and thread_id:
                    state.thread_id = thread_id

            turn_result = await client.request(
                "turn/start",
                {
                    "threadId": state.thread_id,
                    "input": self._build_turn_input(prompt, images),
                    "cwd": str(cwd),
                    **self._turn_overrides(interaction_handler is not None),
                },
            )
            turn = turn_result.get("turn")
            if isinstance(turn, dict):
                turn_id = turn.get("id")
                if isinstance(turn_id, str) and turn_id:
                    state.turn_id = turn_id

            if cancel_event is not None:
                cancel_task = asyncio.create_task(self._watch_cancel(cancel_event, client, state, turn_done))

            await turn_done
        except FileNotFoundError as exc:
            return AgentRunResult(
                thread_id=None,
                answer=self._format_spawn_error(exc),
                stderr_text=str(exc),
                return_code=127,
            )
        except _JsonRpcError as exc:
            stderr_text = "\n".join(stderr_lines).strip()
            return AgentRunResult(
                thread_id=state.thread_id,
                answer=exc.message,
                stderr_text=stderr_text,
                return_code=1,
            )
        finally:
            if cancel_task is not None:
                cancel_task.cancel()
                try:
                    await cancel_task
                except asyncio.CancelledError:
                    pass
            await client.close()

        stderr_text = "\n".join(stderr_lines).strip()
        answer = self._compose_agent_text(state.item_texts, state.item_order)
        return_code = 0

        if state.turn_status == "interrupted":
            return_code = 130
            if not answer:
                answer = "执行已取消。"
        elif state.turn_status == "failed":
            return_code = 1
            if state.turn_error:
                answer = state.turn_error
            elif not answer:
                answer = "Codex 执行失败。"
        elif not answer:
            stdout_text = "\n".join(stdout_lines)
            merged = (stdout_text + "\n" + stderr_text).strip()
            answer = merged[-3500:] if merged else "Codex 没有返回可展示内容。"

        return AgentRunResult(
            thread_id=state.thread_id,
            answer=answer,
            stderr_text=stderr_text,
            return_code=return_code,
        )

    @staticmethod
    async def _watch_cancel(
        cancel_event: asyncio.Event,
        client: _AppServerClient,
        state: _TurnState,
        turn_done: asyncio.Future[None],
    ) -> None:
        await cancel_event.wait()
        if turn_done.done():
            return
        if not state.thread_id or not state.turn_id:
            return
        try:
            await client.request(
                "turn/interrupt",
                {
                    "threadId": state.thread_id,
                    "turnId": state.turn_id,
                },
            )
        except Exception:
            return

    def _thread_params(
        self,
        cwd: Path,
        session_id: Optional[str],
        interactive: bool,
    ) -> dict[str, Any]:
        approval_policy = self._effective_approval_policy(interactive)
        sandbox_mode = self._effective_sandbox_mode()
        if session_id:
            params: dict[str, Any] = {
                "threadId": session_id,
                "persistExtendedHistory": False,
                "cwd": str(cwd),
            }
            if approval_policy is not None:
                params["approvalPolicy"] = approval_policy
            if sandbox_mode is not None:
                params["sandbox"] = sandbox_mode
            return params

        params = {
            "cwd": str(cwd),
            "experimentalRawEvents": False,
            "persistExtendedHistory": False,
        }
        if approval_policy is not None:
            params["approvalPolicy"] = approval_policy
        if sandbox_mode is not None:
            params["sandbox"] = sandbox_mode
        return params

    def _turn_overrides(self, interactive: bool) -> dict[str, Any]:
        approval_policy = self._effective_approval_policy(interactive)
        params: dict[str, Any] = {}
        if approval_policy is not None:
            params["approvalPolicy"] = approval_policy
        return params

    def _effective_approval_policy(self, interactive: bool) -> Optional[str]:
        if self.dangerous_bypass_level >= 2:
            return "never"
        if self.dangerous_bypass_level == 1:
            return self.approval_policy or "never"
        if self.approval_policy:
            return self.approval_policy
        if interactive:
            return "on-request"
        return None

    def _effective_sandbox_mode(self) -> Optional[str]:
        if self.dangerous_bypass_level >= 1:
            return self.sandbox_mode or "danger-full-access"
        return self.sandbox_mode

    @staticmethod
    def _build_turn_input(prompt: str, images: tuple[PromptImage, ...]) -> list[dict[str, Any]]:
        inputs: list[dict[str, Any]] = [
            {"type": "text", "text": prompt, "text_elements": []},
        ]
        for image in images:
            inputs.append({"type": "localImage", "path": str(image.path)})
        return inputs

    async def _request_user_input_response(
        self,
        params: dict[str, Any],
        interaction_handler: Optional[InteractionHandlerProtocol],
    ) -> dict[str, Any]:
        answers: dict[str, dict[str, list[str]]] = {}
        raw_questions = params.get("questions")
        if not isinstance(raw_questions, list):
            return {"answers": answers}
        if interaction_handler is None and raw_questions:
            log("[warn] codex requested user input but no interaction handler is configured; replying with empty answers")

        for question_index, raw_question in enumerate(raw_questions, start=1):
            if not isinstance(raw_question, dict):
                continue
            question_id = self._string_or_none(raw_question.get("id"))
            if not question_id:
                continue
            question_text = self._string_or_none(raw_question.get("question")) or "请提供所需信息。"
            header = self._string_or_none(raw_question.get("header")) or f"问题 {question_index}"
            is_other = bool(raw_question.get("isOther"))
            options = self._question_options(raw_question.get("options"))
            request = QuestionRequest(
                title=header,
                body=self._question_body(question_text, options, is_other),
                options=options,
                reply_mode="buttons" if options and not is_other else "text",
            )
            user_answers: Optional[list[str]] = None
            if interaction_handler is not None:
                user_answers = await interaction_handler.request_question(request)
            answers[question_id] = {"answers": user_answers or []}

        return {"answers": answers}

    @staticmethod
    def _question_options(value: Any) -> tuple[InteractionOption, ...]:
        if not isinstance(value, list):
            return ()
        options: list[InteractionOption] = []
        for index, raw_option in enumerate(value, start=1):
            if not isinstance(raw_option, dict):
                continue
            label = str(raw_option.get("label") or "").strip()
            if not label:
                continue
            description = str(raw_option.get("description") or "").strip()
            options.append(
                InteractionOption(
                    id=f"opt{index}",
                    label=label,
                    description=description,
                )
            )
        return tuple(options)

    @staticmethod
    def _question_body(question_text: str, options: tuple[InteractionOption, ...], is_other: bool) -> str:
        # When Codex marks `isOther`, the predefined options are only guidance and the
        # user is expected to respond with free text instead of tapping a fixed choice.
        if not options or not is_other:
            return question_text
        labels = " / ".join(option.label for option in options)
        return f"{question_text}\n\n可选值: {labels}"

    @staticmethod
    def _string_or_none(value: Any) -> Optional[str]:
        if isinstance(value, str) and value.strip():
            return value.strip()
        return None

    @staticmethod
    def _compose_agent_text(agent_items: dict[str, str], item_order: list[str]) -> str:
        messages: list[str] = []
        for item_id in item_order:
            text = (agent_items.get(item_id) or "").strip()
            if text:
                messages.append(text)
        return "\n\n".join(messages).strip()

    @staticmethod
    def _compose_reasoning_text(reasoning_parts: dict[str, list[str]]) -> str:
        chunks: list[str] = []
        for item_id in reasoning_parts:
            parts = [part.strip() for part in reasoning_parts[item_id] if part.strip()]
            if parts:
                chunks.append(" ".join(parts))
        return " ".join(chunks).strip()

    @staticmethod
    def _normalize_reasoning_text(text: str) -> str:
        normalized = (text or "").strip()
        if not normalized:
            return ""
        normalized = normalized.replace("\n", " ")
        normalized = re.sub(r"\s+", " ", normalized)
        normalized = re.sub(r"[`*_~>#]+", "", normalized)
        normalized = normalized.strip()
        if len(normalized) > 120:
            normalized = normalized[:119].rstrip() + "…"
        return normalized
