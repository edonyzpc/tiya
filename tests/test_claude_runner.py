import asyncio
from pathlib import Path

import pytest

from services.claude_runner import ClaudeRunner


class _FakeStream:
    def __init__(self, lines: list[str]):
        self._items = [(line + "\n").encode("utf-8") for line in lines]

    async def readline(self) -> bytes:
        await asyncio.sleep(0)
        if not self._items:
            return b""
        return self._items.pop(0)


class _FakeProcess:
    def __init__(self, stdout_lines: list[str], stderr_lines: list[str], return_code: int):
        self.stdout = _FakeStream(stdout_lines)
        self.stderr = _FakeStream(stderr_lines)
        self._return_code = return_code

    async def wait(self) -> int:
        await asyncio.sleep(0)
        return self._return_code


@pytest.mark.asyncio
async def test_run_prompt_stream_extracts_session_partial_and_reasoning(monkeypatch, tmp_path: Path):
    proc = _FakeProcess(
        stdout_lines=[
            '{"type":"system","session_id":"session-claude-1"}',
            '{"type":"stream_event","session_id":"session-claude-1","event":{"type":"content_block_delta","delta":{"type":"thinking_delta","thinking":"**Planning**"}}}',
            '{"type":"stream_event","session_id":"session-claude-1","event":{"type":"content_block_delta","delta":{"type":"text_delta","text":"Hello"}}}',
            '{"type":"stream_event","session_id":"session-claude-1","event":{"type":"content_block_delta","delta":{"type":"text_delta","text":" world"}}}',
            '{"type":"result","session_id":"session-claude-1","result":"Hello world"}',
        ],
        stderr_lines=[],
        return_code=0,
    )

    async def _fake_create(*args, **kwargs):
        return proc

    monkeypatch.setattr("services.claude_runner.asyncio.create_subprocess_exec", _fake_create)

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
async def test_run_prompt_resume_uses_r_flag(monkeypatch, tmp_path: Path):
    proc = _FakeProcess(
        stdout_lines=['{"type":"result","session_id":"sid-2","result":"ok"}'],
        stderr_lines=[],
        return_code=0,
    )
    captured_args: list[str] = []

    async def _fake_create(*args, **kwargs):
        captured_args.extend([str(v) for v in args])
        return proc

    monkeypatch.setattr("services.claude_runner.asyncio.create_subprocess_exec", _fake_create)

    runner = ClaudeRunner(claude_bin="claude", model="sonnet", permission_mode="default")
    result = await runner.run_prompt("hello", tmp_path, session_id="resume-sid")

    assert result.return_code == 0
    assert "-r" in captured_args
    idx = captured_args.index("-r")
    assert captured_args[idx + 1] == "resume-sid"


@pytest.mark.asyncio
async def test_run_prompt_non_zero_exit_uses_merged_output(monkeypatch, tmp_path: Path):
    proc = _FakeProcess(
        stdout_lines=["raw stdout line"],
        stderr_lines=["stderr line"],
        return_code=2,
    )

    async def _fake_create(*args, **kwargs):
        return proc

    monkeypatch.setattr("services.claude_runner.asyncio.create_subprocess_exec", _fake_create)

    runner = ClaudeRunner(claude_bin="claude")
    result = await runner.run_prompt("hi", tmp_path)

    assert result.return_code == 2
    assert "raw stdout line" in result.answer
    assert "stderr line" in result.stderr_text


@pytest.mark.asyncio
async def test_run_prompt_handles_missing_binary(monkeypatch, tmp_path: Path):
    async def _fake_create(*args, **kwargs):
        raise FileNotFoundError("not found")

    monkeypatch.setattr("services.claude_runner.asyncio.create_subprocess_exec", _fake_create)

    runner = ClaudeRunner(claude_bin="/missing/claude")
    result = await runner.run_prompt("hi", tmp_path)

    assert result.return_code == 127
    assert "找不到 claude 可执行文件" in result.answer


@pytest.mark.asyncio
async def test_run_prompt_handles_missing_cwd(monkeypatch, tmp_path: Path):
    async def _fake_create(*args, **kwargs):
        raise FileNotFoundError(2, "No such file or directory", "tiya")

    monkeypatch.setattr("services.claude_runner.asyncio.create_subprocess_exec", _fake_create)

    runner = ClaudeRunner(claude_bin="claude")
    result = await runner.run_prompt("hi", tmp_path / "missing-cwd")

    assert result.return_code == 127
    assert "工作目录不存在或不可访问: tiya" in result.answer
