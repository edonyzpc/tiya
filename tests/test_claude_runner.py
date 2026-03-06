import asyncio
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

import pytest

from src.domain.models import ApprovalRequest, PromptImage, QuestionRequest
from src.services.claude_runner import ClaudeRunner, _SDKSymbols


@dataclass
class _FakeTextBlock:
    text: str


@dataclass
class _FakeAssistantMessage:
    content: list[Any]
    model: str = "claude-test"


@dataclass
class _FakeResultMessage:
    session_id: str
    result: Optional[str] = None
    is_error: bool = False
    subtype: str = "result"
    duration_ms: int = 1
    duration_api_ms: int = 1
    num_turns: int = 1
    stop_reason: Optional[str] = None


@dataclass
class _FakeStreamEvent:
    event: dict[str, Any]
    session_id: str = "session-1"
    uuid: str = "uuid-1"


@dataclass
class _FakePermissionResultAllow:
    behavior: str = "allow"
    updated_input: Optional[dict[str, Any]] = None
    updated_permissions: Optional[list[Any]] = None


@dataclass
class _FakePermissionResultDeny:
    behavior: str = "deny"
    message: str = ""
    interrupt: bool = False


@dataclass
class _FakeHookMatcher:
    matcher: Optional[str] = None
    hooks: list[Any] = None
    timeout: Optional[float] = None

    def __post_init__(self):
        if self.hooks is None:
            self.hooks = []


class _FakeClaudeAgentOptions:
    def __init__(self, **kwargs):
        for key, value in kwargs.items():
            setattr(self, key, value)


class _FakeClaudeSDKClient:
    messages: list[Any] = []
    receive_callback = None
    last_instance: Optional["_FakeClaudeSDKClient"] = None

    def __init__(self, options):
        self.options = options
        self.connected = False
        self.query_calls: list[tuple[str, str]] = []
        self.interrupted = False
        _FakeClaudeSDKClient.last_instance = self

    async def connect(self):
        self.connected = True

    async def query(self, prompt: str, session_id: str = "default"):
        self.query_calls.append((prompt, session_id))

    async def receive_response(self):
        if _FakeClaudeSDKClient.receive_callback is not None:
            async for item in _FakeClaudeSDKClient.receive_callback(self):
                yield item
            return
        for message in _FakeClaudeSDKClient.messages:
            yield message

    async def interrupt(self):
        self.interrupted = True

    async def disconnect(self):
        self.connected = False


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


def _fake_sdk() -> _SDKSymbols:
    return _SDKSymbols(
        ClaudeSDKClient=_FakeClaudeSDKClient,
        ClaudeAgentOptions=_FakeClaudeAgentOptions,
        HookMatcher=_FakeHookMatcher,
        PermissionResultAllow=_FakePermissionResultAllow,
        PermissionResultDeny=_FakePermissionResultDeny,
        AssistantMessage=_FakeAssistantMessage,
        ResultMessage=_FakeResultMessage,
        StreamEvent=_FakeStreamEvent,
        TextBlock=_FakeTextBlock,
    )


@pytest.mark.asyncio
async def test_run_prompt_stream_extracts_session_partial_and_reasoning(monkeypatch, tmp_path: Path):
    monkeypatch.setattr("src.services.claude_runner._load_sdk", _fake_sdk)
    _FakeClaudeSDKClient.receive_callback = None
    _FakeClaudeSDKClient.messages = [
        _FakeStreamEvent({"type": "content_block_delta", "delta": {"type": "thinking_delta", "thinking": "**Planning**"}}),
        _FakeStreamEvent({"type": "content_block_delta", "delta": {"type": "text_delta", "text": "Hello"}}),
        _FakeStreamEvent({"type": "content_block_delta", "delta": {"type": "text_delta", "text": " world"}}),
        _FakeResultMessage(session_id="session-claude-1", result="Hello world"),
    ]

    partials: list[str] = []
    reasoning: list[str] = []

    async def _on_partial(text: str) -> None:
        partials.append(text)

    async def _on_reasoning(text: str) -> None:
        reasoning.append(text)

    runner = ClaudeRunner(claude_bin="claude")
    result = await runner.run_prompt("hi", tmp_path, on_partial=_on_partial, on_reasoning=_on_reasoning)

    assert result.thread_id == "session-claude-1"
    assert result.answer == "Hello world"
    assert result.return_code == 0
    assert partials[-1] == "Hello world"
    assert reasoning[-1] == "Planning"


@pytest.mark.asyncio
async def test_run_prompt_resume_uses_sdk_resume_and_image_dirs(monkeypatch, tmp_path: Path):
    monkeypatch.setattr("src.services.claude_runner._load_sdk", _fake_sdk)
    image_path = tmp_path / "attachments" / "image.png"
    _FakeClaudeSDKClient.receive_callback = None
    _FakeClaudeSDKClient.messages = [_FakeResultMessage(session_id="sid-2", result="ok")]

    runner = ClaudeRunner(claude_bin="claude", model="sonnet", permission_mode="default")
    result = await runner.run_prompt(
        "describe this image",
        tmp_path,
        session_id="resume-sid",
        images=(PromptImage(path=image_path, file_name="image.png", mime_type="image/png", file_size=123),),
    )

    client = _FakeClaudeSDKClient.last_instance
    assert result.return_code == 0
    assert client is not None
    assert client.options.resume == "resume-sid"
    assert client.options.model == "sonnet"
    assert list(client.options.add_dirs) == [image_path.parent]
    assert client.query_calls[-1][1] == "resume-sid"
    assert str(image_path) in client.query_calls[-1][0]


@pytest.mark.asyncio
async def test_run_prompt_handles_tool_approval(monkeypatch, tmp_path: Path):
    monkeypatch.setattr("src.services.claude_runner._load_sdk", _fake_sdk)
    handler = _FakeInteractionHandler(approval_decision="accept")

    async def _receive(client):
        response = await client.options.can_use_tool(
            "Bash",
            {"command": "pwd", "cwd": str(tmp_path), "description": "inspect cwd"},
            SimpleNamespace(suggestions=[]),
        )
        client.tool_response = response
        yield _FakeResultMessage(session_id="sid-approval", result="ok")

    _FakeClaudeSDKClient.receive_callback = _receive
    _FakeClaudeSDKClient.messages = []

    runner = ClaudeRunner(claude_bin="claude")
    result = await runner.run_prompt("hi", tmp_path, interaction_handler=handler)

    client = _FakeClaudeSDKClient.last_instance
    assert result.answer == "ok"
    assert handler.approvals
    assert handler.approvals[0].command == "pwd"
    assert client is not None
    assert isinstance(client.tool_response, _FakePermissionResultAllow)
    assert client.tool_response.updated_input["command"] == "pwd"


@pytest.mark.asyncio
async def test_run_prompt_handles_ask_user_question(monkeypatch, tmp_path: Path):
    monkeypatch.setattr("src.services.claude_runner._load_sdk", _fake_sdk)
    handler = _FakeInteractionHandler(question_answers=["B"])

    async def _receive(client):
        response = await client.options.can_use_tool(
            "AskUserQuestion",
            {
                "questions": [
                    {
                        "header": "Mode",
                        "question": "Pick one",
                        "options": ["A", "B"],
                        "multiSelect": False,
                    }
                ]
            },
            SimpleNamespace(suggestions=[]),
        )
        client.tool_response = response
        yield _FakeResultMessage(session_id="sid-question", result="done")

    _FakeClaudeSDKClient.receive_callback = _receive
    _FakeClaudeSDKClient.messages = []

    runner = ClaudeRunner(claude_bin="claude")
    result = await runner.run_prompt("hi", tmp_path, interaction_handler=handler)

    client = _FakeClaudeSDKClient.last_instance
    assert result.answer == "done"
    assert handler.questions
    assert handler.questions[0].title == "Mode"
    assert client is not None
    assert isinstance(client.tool_response, _FakePermissionResultAllow)
    assert client.tool_response.updated_input["answers"]["Pick one"] == "B"


@pytest.mark.asyncio
async def test_run_prompt_cancel_interrupts_client(monkeypatch, tmp_path: Path):
    monkeypatch.setattr("src.services.claude_runner._load_sdk", _fake_sdk)
    cancel_event = asyncio.Event()
    gate = asyncio.Event()

    async def _receive(client):
        gate.set()
        while not client.interrupted:
            await asyncio.sleep(0)
        yield _FakeResultMessage(session_id="sid-cancel", result=None)

    _FakeClaudeSDKClient.receive_callback = _receive
    _FakeClaudeSDKClient.messages = []

    runner = ClaudeRunner(claude_bin="claude")
    task = asyncio.create_task(runner.run_prompt("hi", tmp_path, cancel_event=cancel_event))
    await gate.wait()
    cancel_event.set()
    result = await task

    client = _FakeClaudeSDKClient.last_instance
    assert client is not None
    assert client.interrupted is True
    assert result.return_code == 130


@pytest.mark.asyncio
async def test_watch_cancel_ignores_interrupt_errors():
    cancel_event = asyncio.Event()
    cancel_event.set()

    class _BrokenClient:
        async def interrupt(self):
            raise RuntimeError("boom")

    await ClaudeRunner._watch_cancel(cancel_event, _BrokenClient())


@pytest.mark.asyncio
async def test_run_prompt_handles_missing_binary(tmp_path: Path):
    runner = ClaudeRunner(claude_bin="/missing/claude")
    result = await runner.run_prompt("hi", tmp_path)

    assert result.return_code == 127
    assert "找不到 claude 可执行文件" in result.answer


@pytest.mark.asyncio
async def test_run_prompt_handles_missing_cwd(tmp_path: Path):
    runner = ClaudeRunner(claude_bin="claude")
    result = await runner.run_prompt("hi", tmp_path / "missing-cwd")

    assert result.return_code == 127
    assert "工作目录不存在或不可访问" in result.answer
