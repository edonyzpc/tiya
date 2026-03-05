import asyncio
import json
import re
import subprocess
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from domain.models import AgentRunResult


class CodexRunner:
    def __init__(
        self,
        codex_bin: str,
        sandbox_mode: Optional[str] = None,
        approval_policy: Optional[str] = None,
        dangerous_bypass_level: int = 0,
    ):
        self.codex_bin = codex_bin
        self.sandbox_mode = sandbox_mode
        self.approval_policy = approval_policy
        self.dangerous_bypass_level = max(0, min(2, int(dangerous_bypass_level)))

    @staticmethod
    def _to_toml_string(value: str) -> str:
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'

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
        on_partial: Optional[Callable[[str], Awaitable[None]]] = None,
        on_reasoning: Optional[Callable[[str], Awaitable[None]]] = None,
    ) -> AgentRunResult:
        config_flags: list[str] = []
        if self.dangerous_bypass_level == 1:
            sandbox_mode = self.sandbox_mode or "danger-full-access"
            approval_policy = self.approval_policy or "never"
            config_flags.extend(["-c", f"sandbox_mode={self._to_toml_string(sandbox_mode)}"])
            config_flags.extend(["-c", f"approval_policy={self._to_toml_string(approval_policy)}"])

        exec_flags: list[str] = ["--json", "--skip-git-repo-check"]
        if self.dangerous_bypass_level >= 2:
            exec_flags.append("--dangerously-bypass-approvals-and-sandbox")

        if session_id:
            cmd = [
                self.codex_bin,
                "exec",
                "resume",
                *config_flags,
                *exec_flags,
                session_id,
                prompt,
            ]
        else:
            cmd = [
                self.codex_bin,
                "exec",
                *config_flags,
                *exec_flags,
                prompt,
            ]

        stdout_lines: list[str] = []
        stderr_lines: list[str] = []
        thread_id: Optional[str] = None
        agent_items: dict[str, str] = {}
        agent_order: list[str] = []
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

            if evt.get("type") == "thread.started":
                value = evt.get("thread_id")
                if isinstance(value, str) and value:
                    thread_id = value

            reasoning_summary = self._extract_reasoning_status(evt)
            if reasoning_summary and reasoning_summary != last_reasoning:
                last_reasoning = reasoning_summary
                if on_reasoning is not None:
                    try:
                        await on_reasoning(reasoning_summary)
                    except Exception:
                        pass

            if self._update_agent_stream_state(evt, agent_items, agent_order):
                partial = self._compose_agent_text(agent_items, agent_order)
                if partial and partial != last_partial:
                    last_partial = partial
                    if on_partial is not None:
                        try:
                            await on_partial(partial)
                        except Exception:
                            pass

        return_code = await proc.wait()
        await stderr_task

        stdout_text = "\n".join(stdout_lines)
        stderr_text = "\n".join(stderr_lines).strip()
        agent_text = self._compose_agent_text(agent_items, agent_order)
        parsed_thread_id: Optional[str] = None

        if not agent_text:
            parsed_thread_id, agent_text = self._parse_exec_json(stdout_text)
        if not thread_id and parsed_thread_id:
            thread_id = parsed_thread_id
        if not agent_text:
            merged = (stdout_text + "\n" + stderr_text).strip()
            if merged:
                agent_text = merged[-3500:]
            else:
                agent_text = "Codex 没有返回可展示内容。"

        return AgentRunResult(
            thread_id=thread_id,
            answer=agent_text,
            stderr_text=stderr_text,
            return_code=return_code,
        )

    @staticmethod
    def _compose_agent_text(agent_items: dict[str, str], item_order: list[str]) -> str:
        messages: list[str] = []
        for item_id in item_order:
            text = (agent_items.get(item_id) or "").strip()
            if text:
                messages.append(text)
        return "\n\n".join(messages).strip()

    @staticmethod
    def _extract_text_from_content(content: Any) -> str:
        if not isinstance(content, list):
            return ""
        parts: list[str] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            text = block.get("text")
            if isinstance(text, str):
                parts.append(text)
                continue
            if isinstance(text, dict):
                value = text.get("value")
                if isinstance(value, str):
                    parts.append(value)
                    continue
            value = block.get("value")
            if isinstance(value, str):
                parts.append(value)
        return "".join(parts)

    @staticmethod
    def _merge_partial_text(previous: str, incoming: str) -> str:
        if not previous:
            return incoming
        if incoming.startswith(previous):
            return incoming
        return previous + incoming

    @classmethod
    def _extract_reasoning_text(cls, value: Any) -> str:
        if isinstance(value, str):
            return value
        if isinstance(value, list):
            return cls._extract_text_from_content(value)
        if not isinstance(value, dict):
            return ""

        direct_text = value.get("text")
        if isinstance(direct_text, str):
            return direct_text
        if isinstance(direct_text, dict):
            nested_value = direct_text.get("value")
            if isinstance(nested_value, str):
                return nested_value

        for key in ("summary", "content", "value"):
            nested = value.get(key)
            extracted = cls._extract_reasoning_text(nested)
            if extracted:
                return extracted
        return ""

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

    @classmethod
    def _extract_reasoning_status(cls, evt: dict[str, Any]) -> Optional[str]:
        evt_type = str(evt.get("type") or "")
        if "item.completed" not in evt_type:
            return None

        item_obj = evt.get("item")
        if not isinstance(item_obj, dict) or item_obj.get("type") != "reasoning":
            return None

        raw = cls._extract_reasoning_text(item_obj.get("summary"))
        if not raw:
            raw = cls._extract_reasoning_text(item_obj.get("text"))
        if not raw:
            raw = cls._extract_reasoning_text(item_obj)

        normalized = cls._normalize_reasoning_text(raw)
        return normalized or None

    @classmethod
    def _update_agent_stream_state(
        cls,
        evt: dict[str, Any],
        agent_items: dict[str, str],
        item_order: list[str],
    ) -> bool:
        evt_type = str(evt.get("type") or "")
        item_obj = evt.get("item")
        item = item_obj if isinstance(item_obj, dict) else {}
        item_type = item.get("type") or evt.get("item_type")

        is_agent_event = bool(
            item_type == "agent_message"
            or "agent_message" in evt_type
            or "output_text" in evt_type
        )
        if not is_agent_event:
            return False

        item_id = item.get("id") or evt.get("item_id") or "__agent_message__"
        item_id = str(item_id)
        if item_id not in agent_items:
            agent_items[item_id] = ""
            item_order.append(item_id)

        previous = agent_items[item_id]
        full_text = ""
        delta_text = ""

        item_text = item.get("text")
        if isinstance(item_text, str):
            full_text = item_text
        if not full_text:
            evt_text = evt.get("text")
            if isinstance(evt_text, str):
                full_text = evt_text
        if not full_text:
            full_text = cls._extract_text_from_content(item.get("content"))
        if not full_text:
            full_text = cls._extract_text_from_content(evt.get("content"))

        delta = evt.get("delta")
        if isinstance(delta, str):
            delta_text = delta
        elif isinstance(delta, dict):
            value = delta.get("text")
            if isinstance(value, str):
                delta_text = value
        if not delta_text:
            value = evt.get("text_delta")
            if isinstance(value, str):
                delta_text = value
        if not delta_text:
            value = item.get("delta")
            if isinstance(value, str):
                delta_text = value

        next_text = previous
        if full_text:
            next_text = full_text
        elif delta_text:
            next_text = cls._merge_partial_text(previous, delta_text)

        if next_text == previous:
            return False
        agent_items[item_id] = next_text
        return True

    @staticmethod
    def _parse_exec_json(stdout: str) -> tuple[Optional[str], str]:
        thread_id: Optional[str] = None
        messages: list[str] = []
        for line in stdout.splitlines():
            line = line.strip()
            if not line or not line.startswith("{"):
                continue
            try:
                evt = json.loads(line)
            except json.JSONDecodeError:
                continue
            if evt.get("type") == "thread.started":
                value = evt.get("thread_id")
                if isinstance(value, str):
                    thread_id = value
            if evt.get("type") == "item.completed":
                item = evt.get("item") or {}
                if item.get("type") == "agent_message":
                    text = item.get("text")
                    if isinstance(text, str) and text:
                        messages.append(text)
        return thread_id, "\n\n".join(messages).strip()
