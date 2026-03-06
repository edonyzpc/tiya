import asyncio
from collections import deque
from dataclasses import dataclass
import importlib
import re
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from ..domain.models import AgentRunResult, ApprovalRequest, InteractionOption, PromptImage, QuestionRequest
from .runner_protocol import InteractionHandlerProtocol

MAX_CAPTURED_LINES = 200


@dataclass(frozen=True)
class _SDKSymbols:
    ClaudeSDKClient: Any
    ClaudeAgentOptions: Any
    HookMatcher: Any
    PermissionResultAllow: Any
    PermissionResultDeny: Any
    AssistantMessage: Any
    ResultMessage: Any
    StreamEvent: Any
    TextBlock: Any


def _load_sdk() -> _SDKSymbols:
    sdk_client = importlib.import_module("claude_agent_sdk.client")
    sdk_types = importlib.import_module("claude_agent_sdk.types")
    return _SDKSymbols(
        ClaudeSDKClient=sdk_client.ClaudeSDKClient,
        ClaudeAgentOptions=sdk_types.ClaudeAgentOptions,
        HookMatcher=sdk_types.HookMatcher,
        PermissionResultAllow=sdk_types.PermissionResultAllow,
        PermissionResultDeny=sdk_types.PermissionResultDeny,
        AssistantMessage=sdk_types.AssistantMessage,
        ResultMessage=sdk_types.ResultMessage,
        StreamEvent=sdk_types.StreamEvent,
        TextBlock=sdk_types.TextBlock,
    )


class ClaudeRunner:
    def __init__(
        self,
        claude_bin: str,
        model: Optional[str] = None,
        permission_mode: str = "default",
    ):
        self.claude_bin = claude_bin
        self.model = (model or "").strip() or None
        self.permission_mode = (permission_mode or "default").strip() or "default"

    def _format_spawn_error(self, exc: FileNotFoundError) -> str:
        filename = str(getattr(exc, "filename", "") or "").strip()
        bin_candidates = {
            self.claude_bin,
            str(Path(self.claude_bin).expanduser()),
            Path(self.claude_bin).name,
        }
        if filename and filename not in bin_candidates:
            return f"工作目录不存在或不可访问: {filename}"
        return f"找不到 claude 可执行文件: {self.claude_bin}"

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
        if not cwd.exists() or not cwd.is_dir():
            return AgentRunResult(
                thread_id=None,
                answer=f"工作目录不存在或不可访问: {cwd}",
                stderr_text="",
                return_code=127,
            )

        if "/" in self.claude_bin:
            cli_path = Path(self.claude_bin).expanduser()
            if not cli_path.is_file():
                return AgentRunResult(
                    thread_id=None,
                    answer=f"找不到 claude 可执行文件: {self.claude_bin}",
                    stderr_text="",
                    return_code=127,
                )

        try:
            sdk = _load_sdk()
        except ImportError as exc:
            return AgentRunResult(
                thread_id=None,
                answer="缺少 claude-agent-sdk 依赖，无法启用 Claude Telegram 交互模式。",
                stderr_text=str(exc),
                return_code=127,
            )

        stderr_lines: deque[str] = deque(maxlen=MAX_CAPTURED_LINES)
        assistant_messages: list[str] = []
        partial_text = ""
        reasoning_raw = ""
        last_partial = ""
        last_reasoning = ""
        final_result: Optional[str] = None
        final_session_id = session_id
        interrupted_by_user = False

        def _record_stderr(text: str) -> None:
            stderr_lines.append(text.rstrip("\n"))

        async def _emit_partial(text: str) -> None:
            nonlocal last_partial
            if text and text != last_partial:
                last_partial = text
                if on_partial is not None:
                    try:
                        await on_partial(text)
                    except Exception:
                        pass

        async def _emit_reasoning(text: str) -> None:
            nonlocal last_reasoning
            normalized = self._normalize_reasoning_text(text)
            if normalized and normalized != last_reasoning:
                last_reasoning = normalized
                if on_reasoning is not None:
                    try:
                        await on_reasoning(normalized)
                    except Exception:
                        pass

        async def _noop_hook(_input: Any, _tool_use_id: Optional[str], _context: Any) -> dict[str, Any]:
            return {}

        async def _can_use_tool(tool_name: str, input_data: dict[str, Any], _context: Any) -> Any:
            nonlocal interrupted_by_user
            if tool_name == "AskUserQuestion":
                updated_input = await self._ask_user_question_input(input_data, interaction_handler)
                if updated_input is None:
                    interrupted_by_user = True
                    return sdk.PermissionResultDeny(message="User canceled answering the question", interrupt=True)
                return sdk.PermissionResultAllow(updated_input=updated_input)

            if interaction_handler is None:
                return sdk.PermissionResultDeny(message="Approval is unavailable", interrupt=False)

            decision = await interaction_handler.request_approval(self._tool_approval_request(tool_name, input_data))
            if decision in ("accept", "acceptForSession"):
                return sdk.PermissionResultAllow(updated_input=input_data)
            if decision == "cancel":
                interrupted_by_user = True
                return sdk.PermissionResultDeny(message="User canceled this action", interrupt=True)
            return sdk.PermissionResultDeny(message="User denied this action", interrupt=False)

        options_kwargs: dict[str, Any] = {
            "cli_path": self.claude_bin,
            "cwd": str(cwd),
            "model": self.model,
            "permission_mode": self.permission_mode,
            "resume": session_id,
            "include_partial_messages": True,
            "add_dirs": list(self._image_attachment_roots(images)),
            "stderr": _record_stderr,
        }
        if interaction_handler is not None:
            options_kwargs["can_use_tool"] = _can_use_tool
            options_kwargs["hooks"] = {
                "PreToolUse": [sdk.HookMatcher(hooks=[_noop_hook])],
            }

        client = sdk.ClaudeSDKClient(options=sdk.ClaudeAgentOptions(**options_kwargs))
        cancel_task: Optional[asyncio.Task[None]] = None

        try:
            await client.connect()
            if cancel_event is not None:
                cancel_task = asyncio.create_task(self._watch_cancel(cancel_event, client))
            await client.query(
                self._augment_prompt_with_images(prompt, images),
                session_id=session_id or "default",
            )

            async for message in client.receive_response():
                if isinstance(message, sdk.StreamEvent):
                    event = getattr(message, "event", None)
                    if not isinstance(event, dict):
                        continue
                    if event.get("type") != "content_block_delta":
                        continue
                    delta = event.get("delta")
                    if not isinstance(delta, dict):
                        continue
                    delta_type = delta.get("type")
                    if delta_type == "text_delta":
                        delta_text = delta.get("text")
                        if isinstance(delta_text, str) and delta_text:
                            partial_text += delta_text
                            await _emit_partial(partial_text)
                        continue
                    if delta_type == "thinking_delta":
                        thinking_delta = delta.get("thinking")
                        if isinstance(thinking_delta, str) and thinking_delta:
                            reasoning_raw += thinking_delta
                            await _emit_reasoning(reasoning_raw)
                        continue

                if isinstance(message, sdk.AssistantMessage):
                    text = self._extract_assistant_message_text(message, sdk.TextBlock)
                    if text:
                        assistant_messages.append(text)
                    continue

                if isinstance(message, sdk.ResultMessage):
                    if isinstance(message.session_id, str) and message.session_id:
                        final_session_id = message.session_id
                    if isinstance(message.result, str) and message.result.strip():
                        final_result = message.result.strip()
                    if bool(message.is_error) and (cancel_event is None or not cancel_event.is_set()) and not interrupted_by_user:
                        stderr_lines.append(final_result or "Claude returned an error result.")
        except FileNotFoundError as exc:
            return AgentRunResult(
                thread_id=None,
                answer=self._format_spawn_error(exc),
                stderr_text=str(exc),
                return_code=127,
            )
        except Exception as exc:
            stderr_text = "\n".join(stderr_lines).strip()
            return AgentRunResult(
                thread_id=final_session_id,
                answer=f"调用 Claude 时出现异常: {exc}",
                stderr_text=stderr_text or str(exc),
                return_code=1,
            )
        finally:
            if cancel_task is not None:
                if not cancel_task.done():
                    cancel_task.cancel()
                    try:
                        await cancel_task
                    except asyncio.CancelledError:
                        pass
            await client.disconnect()

        stderr_text = "\n".join(stderr_lines).strip()
        answer = final_result or "\n\n".join(text for text in assistant_messages if text.strip()).strip() or partial_text.strip()
        return_code = 0
        if interrupted_by_user or (cancel_event is not None and cancel_event.is_set()):
            return_code = 130
            if not answer:
                answer = "执行已取消。"
        elif stderr_text and not answer:
            return_code = 1
            answer = stderr_text[-3500:]
        elif not answer:
            answer = "Claude 没有返回可展示内容。"

        return AgentRunResult(
            thread_id=final_session_id,
            answer=answer,
            stderr_text=stderr_text,
            return_code=return_code,
        )

    @staticmethod
    async def _watch_cancel(cancel_event: asyncio.Event, client: Any) -> None:
        await cancel_event.wait()
        try:
            await client.interrupt()
        except Exception:
            return

    @staticmethod
    def _image_attachment_roots(images: tuple[PromptImage, ...]) -> tuple[Path, ...]:
        roots: list[Path] = []
        for image in images:
            parent = image.path.parent
            if parent not in roots:
                roots.append(parent)
        return tuple(roots)

    @staticmethod
    def _augment_prompt_with_images(prompt: str, images: tuple[PromptImage, ...]) -> str:
        if not images:
            return prompt
        lines = [prompt.strip(), "", "请结合以下本地图片完成上面的要求："]
        for image in images:
            lines.append(f"- {image.path}")
        return "\n".join(line for line in lines if line is not None).strip()

    @staticmethod
    def _extract_assistant_message_text(message: Any, text_block_type: Any) -> str:
        content = getattr(message, "content", None)
        if not isinstance(content, list):
            return ""
        parts: list[str] = []
        for block in content:
            if isinstance(block, text_block_type):
                text = getattr(block, "text", None)
                if isinstance(text, str) and text:
                    parts.append(text)
        return "".join(parts).strip()

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

    def _tool_approval_request(self, tool_name: str, input_data: dict[str, Any]) -> ApprovalRequest:
        kind = "command"
        command = None
        cwd = None
        body_lines = [f"工具: {tool_name}"]

        if tool_name == "Bash":
            command = self._string_or_none(input_data.get("command"))
            cwd = self._string_or_none(input_data.get("cwd"))
            description = self._string_or_none(input_data.get("description"))
            if description:
                body_lines.append(description)
        elif tool_name in ("Write", "Edit", "MultiEdit"):
            kind = "file_change"
            path_value = self._string_or_none(input_data.get("file_path")) or self._string_or_none(input_data.get("path"))
            if path_value:
                body_lines.append(f"文件: {path_value}")
            old_string = self._string_or_none(input_data.get("old_string"))
            new_string = self._string_or_none(input_data.get("new_string"))
            if old_string:
                body_lines.append(f"原内容片段: {old_string[:160]}")
            if new_string:
                body_lines.append(f"新内容片段: {new_string[:160]}")
        else:
            summary = self._summarize_input(input_data)
            if summary:
                body_lines.append(summary)

        title = f"Claude 请求使用 {tool_name}"
        return ApprovalRequest(
            kind=kind,  # type: ignore[arg-type]
            title=title,
            body="\n".join(line for line in body_lines if line),
            command=command,
            cwd=cwd,
            allow_accept_for_session=False,
        )

    async def _ask_user_question_input(
        self,
        input_data: dict[str, Any],
        interaction_handler: Optional[InteractionHandlerProtocol],
    ) -> Optional[dict[str, Any]]:
        if interaction_handler is None:
            return None

        raw_questions = input_data.get("questions")
        if not isinstance(raw_questions, list) or not raw_questions:
            question = self._string_or_none(input_data.get("question")) or "Claude 需要你补充信息。"
            answers = await interaction_handler.request_question(
                QuestionRequest(
                    title="Claude 需要更多信息",
                    body=question,
                    options=(),
                    reply_mode="text",
                )
            )
            if answers is None:
                return None
            return {"questions": [{"question": question}], "answers": {question: answers[0] if answers else ""}}

        answers_payload: dict[str, str] = {}
        for index, raw_question in enumerate(raw_questions, start=1):
            if not isinstance(raw_question, dict):
                continue
            question_text = self._string_or_none(raw_question.get("question")) or f"问题 {index}"
            header = self._string_or_none(raw_question.get("header")) or f"问题 {index}"
            multi_select = bool(raw_question.get("multiSelect"))
            options = self._ask_user_question_options(raw_question.get("options"))
            request = QuestionRequest(
                title=header,
                body=self._ask_user_question_body(question_text, options, multi_select),
                options=options if options and not multi_select else (),
                reply_mode="buttons" if options and not multi_select else "text",
            )
            user_answers = await interaction_handler.request_question(request)
            if user_answers is None:
                return None
            answer_text = user_answers[0] if user_answers else ""
            if multi_select:
                normalized = self._parse_multi_select_answer(answer_text, options)
                answers_payload[question_text] = ", ".join(normalized) if normalized else answer_text.strip()
            else:
                answers_payload[question_text] = answer_text.strip()

        return {"questions": raw_questions, "answers": answers_payload}

    @staticmethod
    def _ask_user_question_options(value: Any) -> tuple[InteractionOption, ...]:
        if not isinstance(value, list):
            return ()
        options: list[InteractionOption] = []
        for index, raw_option in enumerate(value, start=1):
            if isinstance(raw_option, str):
                label = raw_option.strip()
                description = ""
            elif isinstance(raw_option, dict):
                label = str(raw_option.get("label") or "").strip()
                description = str(raw_option.get("description") or "").strip()
            else:
                continue
            if not label:
                continue
            options.append(InteractionOption(id=f"opt{index}", label=label, description=description))
        return tuple(options)

    @staticmethod
    def _ask_user_question_body(
        question_text: str,
        options: tuple[InteractionOption, ...],
        multi_select: bool,
    ) -> str:
        if not options:
            return question_text
        labels = ", ".join(f"{index}. {option.label}" for index, option in enumerate(options, start=1))
        if multi_select:
            return f"{question_text}\n\n可选项: {labels}\n请回复一个或多个编号，或直接回复选项文本。"
        return question_text

    @staticmethod
    def _parse_multi_select_answer(answer_text: str, options: tuple[InteractionOption, ...]) -> list[str]:
        raw = answer_text.strip()
        if not raw:
            return []
        tokens = [token.strip() for token in re.split(r"[,，\s]+", raw) if token.strip()]
        if tokens and all(token.isdigit() for token in tokens):
            selected: list[str] = []
            for token in tokens:
                index = int(token)
                if 1 <= index <= len(options):
                    selected.append(options[index - 1].label)
            return selected
        return [raw]

    @staticmethod
    def _summarize_input(input_data: dict[str, Any]) -> str:
        lines: list[str] = []
        for key in ("file_path", "path", "query", "pattern", "content", "description"):
            value = input_data.get(key)
            if isinstance(value, str) and value.strip():
                lines.append(f"{key}: {value.strip()[:200]}")
        if not lines and input_data:
            lines.append(str(input_data)[:400])
        return "\n".join(lines)

    @staticmethod
    def _string_or_none(value: Any) -> Optional[str]:
        if isinstance(value, str) and value.strip():
            return value.strip()
        return None
