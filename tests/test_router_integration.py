import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiogram import Bot, Dispatcher

from src.domain.models import AgentRunResult, StreamConfig
from src.services.session_store import AsyncSessionStore, ClaudeSessionStore, CodexSessionStore
from src.services.state_store import StateStore
from src.telegram.rendering import TelegramMessageRenderer
from src.telegram.router import TgCodexService, build_router


class FakeTelegramClient:
    def __init__(self):
        self.send_message_calls = []
        self.send_message_with_result_calls = []
        self.send_document_calls = []
        self.send_photo_calls = []
        self.send_message_draft_calls = []
        self.edit_message_text_calls = []
        self.delete_message_calls = []
        self.answer_callback_query_calls = []

    async def send_message(
        self,
        chat_id,
        text,
        reply_to=None,
        reply_markup=None,
        message_thread_id=None,
        parse_mode=None,
        entities=None,
        disable_web_page_preview=None,
        link_preview_options=None,
    ):
        self.send_message_calls.append(
            {
                "chat_id": chat_id,
                "text": text,
                "reply_to": reply_to,
                "reply_markup": reply_markup,
                "message_thread_id": message_thread_id,
                "parse_mode": parse_mode,
                "entities": entities,
                "disable_web_page_preview": disable_web_page_preview,
                "link_preview_options": link_preview_options,
            }
        )

    async def send_message_with_result(
        self,
        chat_id,
        text,
        reply_to=None,
        reply_markup=None,
        message_thread_id=None,
        parse_mode=None,
        entities=None,
        disable_web_page_preview=None,
        link_preview_options=None,
    ):
        self.send_message_with_result_calls.append(
            {
                "chat_id": chat_id,
                "text": text,
                "reply_to": reply_to,
                "parse_mode": parse_mode,
                "entities": entities,
            }
        )
        return SimpleNamespace(message_id=777)

    async def send_document(
        self,
        chat_id,
        file_name,
        file_data,
        caption_text=None,
        caption_entities=None,
        reply_to=None,
        reply_markup=None,
        message_thread_id=None,
    ):
        self.send_document_calls.append(
            {
                "chat_id": chat_id,
                "file_name": file_name,
                "file_data": file_data,
                "caption_text": caption_text,
                "caption_entities": caption_entities,
                "reply_to": reply_to,
                "reply_markup": reply_markup,
                "message_thread_id": message_thread_id,
            }
        )
        return SimpleNamespace(message_id=778)

    async def send_photo(
        self,
        chat_id,
        file_name,
        file_data,
        caption_text=None,
        caption_entities=None,
        reply_to=None,
        reply_markup=None,
        message_thread_id=None,
    ):
        self.send_photo_calls.append(
            {
                "chat_id": chat_id,
                "file_name": file_name,
                "file_data": file_data,
                "caption_text": caption_text,
                "caption_entities": caption_entities,
                "reply_to": reply_to,
                "reply_markup": reply_markup,
                "message_thread_id": message_thread_id,
            }
        )
        return SimpleNamespace(message_id=779)

    async def send_message_draft(
        self,
        chat_id,
        draft_id,
        text,
        message_thread_id=None,
        fail_fast_retry_after=False,
    ):
        self.send_message_draft_calls.append(
            {
                "chat_id": chat_id,
                "draft_id": draft_id,
                "text": text,
            }
        )
        return True

    async def edit_message_text(
        self,
        chat_id,
        message_id,
        text,
        fail_fast_retry_after=False,
        parse_mode=None,
        entities=None,
        disable_web_page_preview=None,
        link_preview_options=None,
    ):
        self.edit_message_text_calls.append(
            {
                "chat_id": chat_id,
                "message_id": message_id,
                "text": text,
                "parse_mode": parse_mode,
            }
        )
        return True

    async def delete_message(self, chat_id, message_id):
        self.delete_message_calls.append({"chat_id": chat_id, "message_id": message_id})
        return True

    async def send_chat_action(self, chat_id, action="typing", message_thread_id=None):
        return True

    async def set_my_commands(self, commands, language_code=None):
        return True

    async def set_chat_menu_button_commands(self):
        return True

    async def answer_callback_query(self, callback_query_id, text=None, show_alert=False):
        self.answer_callback_query_calls.append(
            {
                "callback_query_id": callback_query_id,
                "text": text,
                "show_alert": show_alert,
            }
        )
        return True


class FakeRunner:
    def __init__(self, name: str):
        self.name = name
        self.calls = []
        self.next_result = AgentRunResult(
            thread_id=f"{name}-thread-1",
            answer=f"{name}-ok-answer",
            stderr_text="",
            return_code=0,
        )

    async def run_prompt(self, prompt, cwd, session_id=None, on_partial=None, on_reasoning=None):
        self.calls.append(
            {
                "prompt": prompt,
                "cwd": str(cwd),
                "session_id": session_id,
            }
        )
        if on_reasoning is not None:
            await on_reasoning("planning")
        if on_partial is not None:
            await on_partial("partial")
        return self.next_result


@pytest.fixture
def bot():
    return Bot(token="123456:abcdefghijklmnopqrstuvwxyzABCDE")


@pytest.fixture
def session_roots(tmp_path: Path) -> tuple[Path, Path]:
    codex_root = tmp_path / "codex-sessions"
    codex_root.mkdir(parents=True)
    codex_path = codex_root / "s1.jsonl"
    with codex_path.open("w", encoding="utf-8") as f:
        f.write(
            json.dumps(
                {
                    "type": "session_meta",
                    "payload": {
                        "id": "session-1",
                        "timestamp": "2026-03-05T00:00:00Z",
                        "cwd": str(tmp_path),
                    },
                },
                ensure_ascii=False,
            )
            + "\n"
        )
        f.write(
            json.dumps(
                {
                    "type": "event_msg",
                    "payload": {
                        "type": "user_message",
                        "message": "codex title",
                    },
                },
                ensure_ascii=False,
            )
            + "\n"
        )

    claude_root = tmp_path / "claude-projects"
    claude_project = claude_root / "-mnt-code-tiya"
    claude_project.mkdir(parents=True)
    claude_id = "925ffdad-54ba-41ed-8e3b-a7e72843079c"
    claude_path = claude_project / f"{claude_id}.jsonl"
    with claude_path.open("w", encoding="utf-8") as f:
        f.write(
            json.dumps(
                {
                    "type": "user",
                    "sessionId": claude_id,
                    "cwd": str(tmp_path),
                    "timestamp": "2026-03-05T00:00:00Z",
                    "message": {"role": "user", "content": "claude title"},
                },
                ensure_ascii=False,
            )
            + "\n"
        )
        f.write(
            json.dumps(
                {
                    "type": "assistant",
                    "sessionId": claude_id,
                    "cwd": str(tmp_path),
                    "timestamp": "2026-03-05T00:00:01Z",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": "claude answer"}],
                    },
                },
                ensure_ascii=False,
            )
            + "\n"
        )

    return codex_root, claude_root


def _build_service(
    tmp_path: Path,
    session_roots: tuple[Path, Path],
    allowed_user_ids=None,
    allowed_cwd_roots: tuple[Path, ...] = (),
):
    api = FakeTelegramClient()
    codex_runner = FakeRunner("codex")
    claude_runner = FakeRunner("claude")
    state = StateStore(tmp_path / "state.json", default_provider="codex", flush_delay_sec=0.01)
    codex_root, claude_root = session_roots
    renderer = TelegramMessageRenderer(
        enabled=True,
        final_only=True,
        style="strong",
        mode="html",
        link_preview_policy="auto",
        fail_open=True,
        backend="telegramify",
    )

    service = TgCodexService(
        api=api,
        session_stores={
            "codex": AsyncSessionStore(CodexSessionStore(codex_root)),
            "claude": AsyncSessionStore(ClaudeSessionStore(claude_root)),
        },
        state=state,
        runners={
            "codex": codex_runner,
            "claude": claude_runner,
        },
        runner_bins={
            "codex": "codex",
            "claude": "claude",
        },
        default_cwd=tmp_path,
        allowed_user_ids=allowed_user_ids,
        allowed_cwd_roots=allowed_cwd_roots,
        stream_config=StreamConfig(
            enabled=False,
            edit_interval_ms=700,
            min_delta_chars=8,
            thinking_status_interval_ms=900,
            retry_cooldown_ms=15000,
            max_consecutive_preview_errors=2,
            preview_failfast=True,
        ),
        renderer=renderer,
    )
    return service, api, state, codex_runner, claude_runner


async def _feed(dp: Dispatcher, bot: Bot, update: dict):
    await dp.feed_raw_update(bot, update)


def _message_update(update_id: int, text: str, user_id: int = 1, message_id: int = 11) -> dict:
    return {
        "update_id": update_id,
        "message": {
            "message_id": message_id,
            "date": 1,
            "chat": {"id": 101, "type": "private"},
            "from": {"id": user_id, "is_bot": False, "first_name": "u"},
            "text": text,
        },
    }


@pytest.mark.asyncio
async def test_help_command_via_feed_update(bot: Bot, tmp_path: Path, session_roots: tuple[Path, Path]):
    service, api, _, _, _ = _build_service(tmp_path, session_roots)
    dp = Dispatcher()
    dp.include_router(build_router(service))

    await _feed(dp, bot, _message_update(1, "/help"))

    assert api.send_message_calls
    assert "可用命令" in api.send_message_calls[-1]["text"]
    assert "/provider" in api.send_message_calls[-1]["text"]
    assert api.send_message_calls[-1]["parse_mode"] is None
    assert api.send_message_calls[-1]["entities"]
    await service.shutdown()


@pytest.mark.asyncio
async def test_provider_switch_and_runner_dispatch(bot: Bot, tmp_path: Path, session_roots: tuple[Path, Path]):
    service, _, state, codex_runner, claude_runner = _build_service(tmp_path, session_roots)
    dp = Dispatcher()
    dp.include_router(build_router(service))

    await _feed(dp, bot, _message_update(1, "/provider claude"))
    assert await state.get_active_provider(1) == "claude"

    await _feed(dp, bot, _message_update(2, "hello from claude", message_id=12))
    assert len(claude_runner.calls) == 1
    assert len(codex_runner.calls) == 0

    await _feed(dp, bot, _message_update(3, "/provider codex", message_id=13))
    await _feed(dp, bot, _message_update(4, "hello from codex", message_id=14))
    assert len(codex_runner.calls) == 1
    assert await state.get_active_provider(1) == "codex"
    await service.shutdown()


@pytest.mark.asyncio
async def test_sessions_and_callback_switch_are_provider_aware(
    bot: Bot, tmp_path: Path, session_roots: tuple[Path, Path]
):
    service, api, state, _, _ = _build_service(tmp_path, session_roots, allowed_user_ids={1})
    dp = Dispatcher()
    dp.include_router(build_router(service))

    await _feed(dp, bot, _message_update(1, "/sessions 1"))
    assert await state.get_last_session_ids(1, provider="codex") == ["session-1"]

    await _feed(
        dp,
        bot,
        {
            "update_id": 2,
            "callback_query": {
                "id": "cq-1",
                "from": {"id": 1, "is_bot": False, "first_name": "u"},
                "chat_instance": "ci",
                "data": "use:codex:session-1",
                "message": {
                    "message_id": 13,
                    "date": 1,
                    "chat": {"id": 101, "type": "private"},
                    "text": "x",
                },
            },
        },
    )
    assert (await state.get_active(1, provider="codex"))[0] == "session-1"
    assert api.answer_callback_query_calls

    await _feed(dp, bot, _message_update(3, "/provider claude", message_id=14))
    await _feed(dp, bot, _message_update(4, "/sessions 1", message_id=15))
    claude_ids = await state.get_last_session_ids(1, provider="claude")
    assert len(claude_ids) == 1
    assert claude_ids[0] == "925ffdad-54ba-41ed-8e3b-a7e72843079c"
    await service.shutdown()


@pytest.mark.asyncio
async def test_continue_and_new_session_paths(bot: Bot, tmp_path: Path, session_roots: tuple[Path, Path]):
    service, _, state, codex_runner, _ = _build_service(tmp_path, session_roots)
    await state.set_active_session(1, "existing-session", str(tmp_path), provider="codex")
    dp = Dispatcher()
    dp.include_router(build_router(service))

    await _feed(dp, bot, _message_update(1, "continue prompt", message_id=21))
    assert codex_runner.calls[-1]["session_id"] == "existing-session"

    await _feed(dp, bot, _message_update(2, "/new", message_id=22))
    await _feed(dp, bot, _message_update(3, "new prompt", message_id=23))
    assert codex_runner.calls[-1]["session_id"] is None
    await service.shutdown()


@pytest.mark.asyncio
async def test_allowlist_block_applies_to_provider_commands(
    bot: Bot, tmp_path: Path, session_roots: tuple[Path, Path]
):
    service, api, _, codex_runner, claude_runner = _build_service(tmp_path, session_roots, allowed_user_ids={1})
    dp = Dispatcher()
    dp.include_router(build_router(service))

    await _feed(dp, bot, _message_update(1, "/provider claude", user_id=2))
    assert api.send_message_calls
    assert "没有权限" in api.send_message_calls[-1]["text"]
    assert codex_runner.calls == []
    assert claude_runner.calls == []
    await service.shutdown()


@pytest.mark.asyncio
async def test_status_and_history_use_rendered_entities(bot: Bot, tmp_path: Path, session_roots: tuple[Path, Path]):
    service, api, _, _, _ = _build_service(tmp_path, session_roots)
    dp = Dispatcher()
    dp.include_router(build_router(service))

    await _feed(dp, bot, _message_update(1, "/status"))
    await _feed(dp, bot, _message_update(2, "/history session-1 1", message_id=12))

    assert len(api.send_message_calls) >= 2
    status_msg = api.send_message_calls[-2]
    history_msg = api.send_message_calls[-1]
    assert status_msg["parse_mode"] is None
    assert history_msg["parse_mode"] is None
    assert status_msg["entities"]
    assert history_msg["entities"]
    await service.shutdown()


@pytest.mark.asyncio
async def test_new_rejects_cwd_outside_allowed_roots(bot: Bot, tmp_path: Path, session_roots: tuple[Path, Path]):
    service, api, _, _, _ = _build_service(
        tmp_path,
        session_roots,
        allowed_cwd_roots=(tmp_path / "allowed",),
    )
    (tmp_path / "allowed").mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    dp = Dispatcher()
    dp.include_router(build_router(service))

    await _feed(dp, bot, _message_update(1, f"/new {outside}", message_id=31))

    assert api.send_message_calls
    assert "不在允许范围内" in api.send_message_calls[-1]["text"]
    await service.shutdown()
