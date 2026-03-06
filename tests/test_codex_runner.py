import asyncio
import json
from pathlib import Path
from typing import Optional

import pytest

from src.domain.models import ApprovalRequest, PromptImage, QuestionRequest
from src.services.codex_runner import CodexRunner


class _FakeReadable:
    def __init__(self):
        self._queue: asyncio.Queue[bytes] = asyncio.Queue()

    async def readline(self) -> bytes:
        return await self._queue.get()

    def feed_json(self, payload: dict) -> None:
        self._queue.put_nowait((json.dumps(payload) + "\n").encode("utf-8"))

    def feed_line(self, line: str) -> None:
        self._queue.put_nowait((line + "\n").encode("utf-8"))

    def close(self) -> None:
        self._queue.put_nowait(b"")


class _FakeStdin:
    def __init__(self, on_message):
        self.on_message = on_message
        self.messages: list[dict] = []

    def write(self, data: bytes) -> None:
        text = data.decode("utf-8")
        for line in text.splitlines():
            if not line.strip():
                continue
            message = json.loads(line)
            self.messages.append(message)
            self.on_message(message)

    async def drain(self) -> None:
        await asyncio.sleep(0)


class _FakeProcess:
    def __init__(self, on_message):
        self.stdout = _FakeReadable()
        self.stderr = _FakeReadable()
        self.stdin = _FakeStdin(on_message)
        self.returncode = None

    def kill(self) -> None:
        self.returncode = 0
        self.stdout.close()
        self.stderr.close()

    async def wait(self) -> int:
        await asyncio.sleep(0)
        return 0 if self.returncode is None else self.returncode


class _FakeInteractionHandler:
    def __init__(self, approval_decision: str = "accept", question_answers: Optional[list[str]] = None):
        self.approval_decision = approval_decision
        self.question_answers = question_answers or ["answer"]
        self.approvals: list[ApprovalRequest] = []
        self.questions: list[QuestionRequest] = []

    async def request_approval(self, request: ApprovalRequest) -> str:
        self.approvals.append(request)
        return self.approval_decision

    async def request_question(self, request: QuestionRequest) -> Optional[list[str]]:
        self.questions.append(request)
        return self.question_answers


@pytest.mark.asyncio
async def test_run_prompt_streams_partial_text(monkeypatch, tmp_path: Path):
    def _on_message(message: dict) -> None:
        method = message.get("method")
        if method == "initialize":
            proc.stdout.feed_json({"id": message["id"], "result": {"userAgent": "codex-test"}})
        elif method == "thread/start":
            proc.stdout.feed_json({"id": message["id"], "result": {"thread": {"id": "thread-1"}}})
        elif method == "turn/start":
            proc.stdout.feed_json({"id": message["id"], "result": {"turn": {"id": "turn-1", "status": "inProgress", "items": [], "error": None}}})
            proc.stdout.feed_json(
                {
                    "method": "item/started",
                    "params": {
                        "threadId": "thread-1",
                        "turnId": "turn-1",
                        "item": {"type": "agentMessage", "id": "a1", "text": "", "phase": "final_answer"},
                    },
                }
            )
            proc.stdout.feed_json(
                {
                    "method": "item/agentMessage/delta",
                    "params": {"threadId": "thread-1", "turnId": "turn-1", "itemId": "a1", "delta": "Hello"},
                }
            )
            proc.stdout.feed_json(
                {
                    "method": "item/agentMessage/delta",
                    "params": {"threadId": "thread-1", "turnId": "turn-1", "itemId": "a1", "delta": " world"},
                }
            )
            proc.stdout.feed_json(
                {
                    "method": "turn/completed",
                    "params": {
                        "threadId": "thread-1",
                        "turn": {"id": "turn-1", "status": "completed", "items": [], "error": None},
                    },
                }
            )

    proc = _FakeProcess(_on_message)

    async def _fake_create(*args, **kwargs):
        return proc

    monkeypatch.setattr("src.services.codex_runner.asyncio.create_subprocess_exec", _fake_create)

    partials: list[str] = []

    async def _on_partial(text: str) -> None:
        partials.append(text)

    runner = CodexRunner(codex_bin="codex")
    result = await runner.run_prompt("hi", tmp_path, on_partial=_on_partial)

    assert result.thread_id == "thread-1"
    assert result.answer == "Hello world"
    assert result.return_code == 0
    assert partials[-1] == "Hello world"


@pytest.mark.asyncio
async def test_run_prompt_emits_reasoning_summary(monkeypatch, tmp_path: Path):
    def _on_message(message: dict) -> None:
        method = message.get("method")
        if method == "initialize":
            proc.stdout.feed_json({"id": message["id"], "result": {"userAgent": "codex-test"}})
        elif method == "thread/start":
            proc.stdout.feed_json({"id": message["id"], "result": {"thread": {"id": "thread-1"}}})
        elif method == "turn/start":
            proc.stdout.feed_json({"id": message["id"], "result": {"turn": {"id": "turn-1", "status": "inProgress", "items": [], "error": None}}})
            proc.stdout.feed_json(
                {
                    "method": "item/reasoning/summaryTextDelta",
                    "params": {"threadId": "thread-1", "turnId": "turn-1", "itemId": "r1", "summaryIndex": 0, "delta": "**Planning**"},
                }
            )
            proc.stdout.feed_json(
                {
                    "method": "item/reasoning/summaryTextDelta",
                    "params": {"threadId": "thread-1", "turnId": "turn-1", "itemId": "r1", "summaryIndex": 0, "delta": " next step"},
                }
            )
            proc.stdout.feed_json(
                {
                    "method": "item/completed",
                    "params": {
                        "threadId": "thread-1",
                        "turnId": "turn-1",
                        "item": {"type": "agentMessage", "id": "a1", "text": "done", "phase": "final_answer"},
                    },
                }
            )
            proc.stdout.feed_json(
                {
                    "method": "turn/completed",
                    "params": {
                        "threadId": "thread-1",
                        "turn": {"id": "turn-1", "status": "completed", "items": [], "error": None},
                    },
                }
            )

    proc = _FakeProcess(_on_message)

    async def _fake_create(*args, **kwargs):
        return proc

    monkeypatch.setattr("src.services.codex_runner.asyncio.create_subprocess_exec", _fake_create)

    reasoning_updates: list[str] = []

    async def _on_reasoning(text: str) -> None:
        reasoning_updates.append(text)

    runner = CodexRunner(codex_bin="codex")
    result = await runner.run_prompt("hi", tmp_path, on_reasoning=_on_reasoning)

    assert result.answer == "done"
    assert result.return_code == 0
    assert reasoning_updates == ["Planning", "Planning next step"]


@pytest.mark.asyncio
async def test_run_prompt_resume_uses_thread_resume_and_local_images(monkeypatch, tmp_path: Path):
    image = PromptImage(path=tmp_path / "img.png", file_name="img.png", mime_type="image/png", file_size=12)

    def _on_message(message: dict) -> None:
        method = message.get("method")
        if method == "initialize":
            proc.stdout.feed_json({"id": message["id"], "result": {"userAgent": "codex-test"}})
        elif method == "thread/resume":
            proc.stdout.feed_json({"id": message["id"], "result": {"thread": {"id": "thread-resumed"}}})
        elif method == "turn/start":
            proc.stdout.feed_json({"id": message["id"], "result": {"turn": {"id": "turn-1", "status": "inProgress", "items": [], "error": None}}})
            proc.stdout.feed_json(
                {
                    "method": "turn/completed",
                    "params": {
                        "threadId": "thread-resumed",
                        "turn": {"id": "turn-1", "status": "completed", "items": [], "error": None},
                    },
                }
            )

    proc = _FakeProcess(_on_message)

    async def _fake_create(*args, **kwargs):
        return proc

    monkeypatch.setattr("src.services.codex_runner.asyncio.create_subprocess_exec", _fake_create)

    runner = CodexRunner(codex_bin="codex")
    result = await runner.run_prompt("inspect", tmp_path, session_id="sid-1", images=(image,))

    assert result.thread_id == "thread-resumed"
    thread_resume = next(message for message in proc.stdin.messages if message.get("method") == "thread/resume")
    turn_start = next(message for message in proc.stdin.messages if message.get("method") == "turn/start")
    assert thread_resume["params"]["threadId"] == "sid-1"
    assert turn_start["params"]["input"][1] == {"type": "localImage", "path": str(image.path)}


@pytest.mark.asyncio
async def test_run_prompt_handles_command_approval(monkeypatch, tmp_path: Path):
    handler = _FakeInteractionHandler(approval_decision="accept")

    def _on_message(message: dict) -> None:
        method = message.get("method")
        if method == "initialize":
            proc.stdout.feed_json({"id": message["id"], "result": {"userAgent": "codex-test"}})
        elif method == "thread/start":
            proc.stdout.feed_json({"id": message["id"], "result": {"thread": {"id": "thread-1"}}})
        elif method == "turn/start":
            proc.stdout.feed_json({"id": message["id"], "result": {"turn": {"id": "turn-1", "status": "inProgress", "items": [], "error": None}}})
            proc.stdout.feed_json(
                {
                    "id": "req-1",
                    "method": "item/commandExecution/requestApproval",
                    "params": {
                        "threadId": "thread-1",
                        "turnId": "turn-1",
                        "itemId": "cmd-1",
                        "command": "pwd",
                        "cwd": str(tmp_path),
                        "reason": "Need to inspect the working directory.",
                        "availableDecisions": ["accept", "acceptForSession", "decline", "cancel"],
                    },
                }
            )
        elif message.get("id") == "req-1":
            proc.stdout.feed_json(
                {
                    "method": "item/completed",
                    "params": {
                        "threadId": "thread-1",
                        "turnId": "turn-1",
                        "item": {"type": "agentMessage", "id": "a1", "text": "done", "phase": "final_answer"},
                    },
                }
            )
            proc.stdout.feed_json(
                {
                    "method": "turn/completed",
                    "params": {
                        "threadId": "thread-1",
                        "turn": {"id": "turn-1", "status": "completed", "items": [], "error": None},
                    },
                }
            )

    proc = _FakeProcess(_on_message)

    async def _fake_create(*args, **kwargs):
        return proc

    monkeypatch.setattr("src.services.codex_runner.asyncio.create_subprocess_exec", _fake_create)

    runner = CodexRunner(codex_bin="codex")
    result = await runner.run_prompt("hi", tmp_path, interaction_handler=handler)

    assert result.answer == "done"
    assert handler.approvals
    approval = handler.approvals[0]
    assert approval.command == "pwd"
    assert approval.allow_accept_for_session is True
    approval_response = next(message for message in proc.stdin.messages if message.get("id") == "req-1")
    assert approval_response["result"] == {"decision": "accept"}


@pytest.mark.asyncio
async def test_run_prompt_cancel_sends_turn_interrupt(monkeypatch, tmp_path: Path):
    cancel_event = asyncio.Event()
    interrupt_seen = asyncio.Event()

    def _on_message(message: dict) -> None:
        method = message.get("method")
        if method == "initialize":
            proc.stdout.feed_json({"id": message["id"], "result": {"userAgent": "codex-test"}})
        elif method == "thread/start":
            proc.stdout.feed_json({"id": message["id"], "result": {"thread": {"id": "thread-1"}}})
        elif method == "turn/start":
            proc.stdout.feed_json({"id": message["id"], "result": {"turn": {"id": "turn-1", "status": "inProgress", "items": [], "error": None}}})
        elif method == "turn/interrupt":
            interrupt_seen.set()
            proc.stdout.feed_json({"id": message["id"], "result": {}})
            proc.stdout.feed_json(
                {
                    "method": "turn/completed",
                    "params": {
                        "threadId": "thread-1",
                        "turn": {"id": "turn-1", "status": "interrupted", "items": [], "error": None},
                    },
                }
            )

    proc = _FakeProcess(_on_message)

    async def _fake_create(*args, **kwargs):
        return proc

    monkeypatch.setattr("src.services.codex_runner.asyncio.create_subprocess_exec", _fake_create)

    runner = CodexRunner(codex_bin="codex")
    task = asyncio.create_task(runner.run_prompt("hi", tmp_path, cancel_event=cancel_event))
    await asyncio.sleep(0)
    cancel_event.set()
    result = await task

    assert interrupt_seen.is_set()
    assert result.return_code == 130
    interrupt_request = next(message for message in proc.stdin.messages if message.get("method") == "turn/interrupt")
    assert interrupt_request["params"] == {"threadId": "thread-1", "turnId": "turn-1"}


@pytest.mark.asyncio
async def test_run_prompt_handles_missing_binary(monkeypatch, tmp_path: Path):
    async def _fake_create(*args, **kwargs):
        raise FileNotFoundError("not found")

    monkeypatch.setattr("src.services.codex_runner.asyncio.create_subprocess_exec", _fake_create)

    runner = CodexRunner(codex_bin="/missing/codex")
    result = await runner.run_prompt("hi", tmp_path)

    assert result.return_code == 127
    assert "找不到 codex 可执行文件" in result.answer


@pytest.mark.asyncio
async def test_run_prompt_handles_missing_cwd(monkeypatch, tmp_path: Path):
    async def _fake_create(*args, **kwargs):
        raise FileNotFoundError(2, "No such file or directory", "tiya")

    monkeypatch.setattr("src.services.codex_runner.asyncio.create_subprocess_exec", _fake_create)

    runner = CodexRunner(codex_bin="codex")
    result = await runner.run_prompt("hi", tmp_path / "missing-cwd")

    assert result.return_code == 127
    assert "工作目录不存在或不可访问: tiya" in result.answer
