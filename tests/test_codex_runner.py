import asyncio
from pathlib import Path

import pytest

from src.domain.models import PromptImage
from src.services.codex_runner import CodexRunner


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
async def test_run_prompt_stream_extracts_thread_and_partial(monkeypatch, tmp_path: Path):
    proc = _FakeProcess(
        stdout_lines=[
            '{"type":"thread.started","thread_id":"thread-1"}',
            '{"type":"item.delta","item":{"id":"a","type":"agent_message"},"delta":"Hello"}',
            '{"type":"item.delta","item":{"id":"a","type":"agent_message"},"delta":" world"}',
        ],
        stderr_lines=[],
        return_code=0,
    )

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
    proc = _FakeProcess(
        stdout_lines=[
            '{"type":"item.completed","item":{"type":"reasoning","summary":[{"type":"summary_text","text":"**Planning** next step"}]}}',
            '{"type":"item.completed","item":{"type":"reasoning","summary":"**Planning** next step"}}',
            '{"type":"item.completed","item":{"type":"reasoning","summary":"Finalizing output"}}',
            '{"type":"item.completed","item":{"id":"a","type":"agent_message","text":"done"}}',
        ],
        stderr_lines=[],
        return_code=0,
    )

    async def _fake_create(*args, **kwargs):
        return proc

    monkeypatch.setattr("src.services.codex_runner.asyncio.create_subprocess_exec", _fake_create)

    reasoning_updates: list[str] = []

    async def _on_reasoning(text: str) -> None:
        reasoning_updates.append(text)

    runner = CodexRunner(codex_bin="codex")
    result = await runner.run_prompt("hi", tmp_path, on_reasoning=_on_reasoning)

    assert result.return_code == 0
    assert result.answer == "done"
    assert reasoning_updates == ["Planning next step", "Finalizing output"]


@pytest.mark.asyncio
async def test_run_prompt_non_zero_exit_uses_merged_output(monkeypatch, tmp_path: Path):
    proc = _FakeProcess(
        stdout_lines=["raw stdout line"],
        stderr_lines=["stderr line"],
        return_code=2,
    )

    async def _fake_create(*args, **kwargs):
        return proc

    monkeypatch.setattr("src.services.codex_runner.asyncio.create_subprocess_exec", _fake_create)

    runner = CodexRunner(codex_bin="codex")
    result = await runner.run_prompt("hi", tmp_path)

    assert result.return_code == 2
    assert "raw stdout line" in result.answer
    assert "stderr line" in result.stderr_text


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


@pytest.mark.asyncio
async def test_run_prompt_stderr_timeout_does_not_hang(monkeypatch, tmp_path: Path):
    proc = _FakeProcess(
        stdout_lines=['{"type":"item.completed","item":{"id":"a","type":"agent_message","text":"done"}}'],
        stderr_lines=[],
        return_code=0,
    )

    async def _blocked_readline() -> bytes:
        await asyncio.sleep(10)
        return b""

    proc.stderr.readline = _blocked_readline  # type: ignore[method-assign]

    async def _fake_create(*args, **kwargs):
        return proc

    async def _fake_wait_for(awaitable, timeout):
        raise asyncio.TimeoutError

    monkeypatch.setattr("src.services.codex_runner.asyncio.create_subprocess_exec", _fake_create)
    monkeypatch.setattr("src.services.codex_runner.asyncio.wait_for", _fake_wait_for)

    runner = CodexRunner(codex_bin="codex")
    result = await runner.run_prompt("hi", tmp_path)

    assert result.return_code == 0
    assert result.answer == "done"


@pytest.mark.asyncio
async def test_run_prompt_passes_image_flags_for_new_and_resumed_sessions(monkeypatch, tmp_path: Path):
    proc = _FakeProcess(
        stdout_lines=['{"type":"thread.started","thread_id":"thread-2"}', '{"type":"item.completed","item":{"id":"a","type":"agent_message","text":"done"}}'],
        stderr_lines=[],
        return_code=0,
    )
    captured_calls: list[list[str]] = []
    image_path = tmp_path / "test.png"
    image = PromptImage(path=image_path, file_name="test.png", mime_type="image/png", file_size=12)

    async def _fake_create(*args, **kwargs):
        captured_calls.append([str(value) for value in args])
        return proc

    monkeypatch.setattr("src.services.codex_runner.asyncio.create_subprocess_exec", _fake_create)

    runner = CodexRunner(codex_bin="codex")
    await runner.run_prompt("inspect", tmp_path, images=(image,))
    await runner.run_prompt("inspect again", tmp_path, session_id="sid-1", images=(image,))

    assert "--image" in captured_calls[0]
    assert str(image_path) in captured_calls[0]
    assert captured_calls[0][-1] == "inspect"
    assert "resume" in captured_calls[1]
    assert "--image" in captured_calls[1]
    assert str(image_path) in captured_calls[1]
    assert captured_calls[1][-2] == "sid-1"
    assert captured_calls[1][-1] == "inspect again"
