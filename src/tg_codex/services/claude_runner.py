import asyncio
import json
import re
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from tg_codex.domain.models import AgentRunResult


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
        on_partial: Optional[Callable[[str], Awaitable[None]]] = None,
        on_reasoning: Optional[Callable[[str], Awaitable[None]]] = None,
    ) -> AgentRunResult:
        cmd: list[str] = [
            self.claude_bin,
            "-p",
            "--verbose",
            "--output-format",
            "stream-json",
            "--include-partial-messages",
        ]
        if self.model:
            cmd.extend(["--model", self.model])
        if self.permission_mode:
            cmd.extend(["--permission-mode", self.permission_mode])
        if session_id:
            cmd.extend(["-r", session_id])
        cmd.append(prompt)

        stdout_lines: list[str] = []
        stderr_lines: list[str] = []
        thread_id: Optional[str] = None
        final_result: Optional[str] = None
        assistant_items: dict[str, str] = {}
        assistant_order: list[str] = []
        partial_text = ""
        reasoning_raw = ""
        last_partial = ""
        last_reasoning = ""

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=str(cwd),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            return AgentRunResult(
                thread_id=None,
                answer=self._format_spawn_error(exc),
                stderr_text=str(exc),
                return_code=127,
            )

        async def _read_stderr() -> None:
            assert proc.stderr is not None
            while True:
                raw = await proc.stderr.readline()
                if not raw:
                    return
                stderr_lines.append(raw.decode("utf-8", errors="replace").rstrip("\n"))

        stderr_task = asyncio.create_task(_read_stderr())

        assert proc.stdout is not None
        while True:
            raw = await proc.stdout.readline()
            if not raw:
                break
            line_text = raw.decode("utf-8", errors="replace").rstrip("\n")
            stdout_lines.append(line_text)

            line = line_text.strip()
            if not line or not line.startswith("{"):
                continue

            try:
                evt = json.loads(line)
            except json.JSONDecodeError:
                continue

            session_value = evt.get("session_id")
            if isinstance(session_value, str) and session_value:
                thread_id = session_value

            if evt.get("type") == "assistant":
                message = evt.get("message")
                if isinstance(message, dict):
                    item_id = message.get("id")
                    if not isinstance(item_id, str) or not item_id:
                        item_id = str(evt.get("uuid") or "__assistant__")
                    if item_id not in assistant_items:
                        assistant_order.append(item_id)
                        assistant_items[item_id] = ""

                    text = self._extract_message_text(message.get("content"))
                    if text:
                        assistant_items[item_id] = text

            if evt.get("type") == "result":
                value = evt.get("result")
                if isinstance(value, str) and value.strip():
                    final_result = value.strip()

            if evt.get("type") != "stream_event":
                continue
            event_obj = evt.get("event")
            if not isinstance(event_obj, dict):
                continue
            if event_obj.get("type") != "content_block_delta":
                continue
            delta = event_obj.get("delta")
            if not isinstance(delta, dict):
                continue

            delta_type = delta.get("type")
            if delta_type == "text_delta":
                delta_text = delta.get("text")
                if isinstance(delta_text, str) and delta_text:
                    partial_text += delta_text
                    if partial_text != last_partial:
                        last_partial = partial_text
                        if on_partial is not None:
                            try:
                                await on_partial(partial_text)
                            except Exception:
                                pass
                continue

            if delta_type == "thinking_delta":
                thinking_delta = delta.get("thinking")
                if isinstance(thinking_delta, str) and thinking_delta:
                    reasoning_raw += thinking_delta
                    summary = self._normalize_reasoning_text(reasoning_raw)
                    if summary and summary != last_reasoning:
                        last_reasoning = summary
                        if on_reasoning is not None:
                            try:
                                await on_reasoning(summary)
                            except Exception:
                                pass

        return_code = await proc.wait()
        await stderr_task

        stdout_text = "\n".join(stdout_lines)
        stderr_text = "\n".join(stderr_lines).strip()
        assistant_text = self._compose_assistant_text(assistant_items, assistant_order)

        answer = ""
        if final_result:
            answer = final_result
        elif assistant_text:
            answer = assistant_text
        elif partial_text.strip():
            answer = partial_text.strip()

        if not answer:
            merged = (stdout_text + "\n" + stderr_text).strip()
            if merged:
                answer = merged[-3500:]
            else:
                answer = "Claude 没有返回可展示内容。"

        return AgentRunResult(
            thread_id=thread_id,
            answer=answer,
            stderr_text=stderr_text,
            return_code=return_code,
        )

    @staticmethod
    def _extract_message_text(content: Any) -> str:
        if not isinstance(content, list):
            return ""
        parts: list[str] = []
        for item in content:
            if not isinstance(item, dict):
                continue
            if item.get("type") != "text":
                continue
            value = item.get("text")
            if isinstance(value, str) and value:
                parts.append(value)
        return "".join(parts).strip()

    @staticmethod
    def _compose_assistant_text(assistant_items: dict[str, str], item_order: list[str]) -> str:
        messages: list[str] = []
        for item_id in item_order:
            text = (assistant_items.get(item_id) or "").strip()
            if text:
                messages.append(text)
        return "\n\n".join(messages).strip()

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
