import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiogram import Bot, Dispatcher

from tg_codex.domain.models import CodexRunResult
from tg_codex.services.session_store import SessionStore
from tg_codex.services.state_store import StateStore
from tg_codex.telegram.router import TgCodexService, build_router


class FakeTelegramClient:
    def __init__(self):
        self.send_message_calls = []
        self.send_message_with_result_calls = []
        self.send_message_draft_calls = []
        self.edit_message_text_calls = []
        self.delete_message_calls = []
        self.answer_callback_query_calls = []

    async def send_message(self, chat_id, text, reply_to=None, reply_markup=None, message_thread_id=None):
        self.send_message_calls.append(
            {
                "chat_id": chat_id,
                "text": text,
                "reply_to": reply_to,
                "reply_markup": reply_markup,
                "message_thread_id": message_thread_id,
            }
        )

    async def send_message_with_result(self, chat_id, text, reply_to=None, reply_markup=None, message_thread_id=None):
        self.send_message_with_result_calls.append(
            {
                "chat_id": chat_id,
                "text": text,
                "reply_to": reply_to,
            }
        )
        return SimpleNamespace(message_id=777)

    async def send_message_draft(self, chat_id, draft_id, text, message_thread_id=None):
        self.send_message_draft_calls.append(
            {
                "chat_id": chat_id,
                "draft_id": draft_id,
                "text": text,
            }
        )
        return True

    async def edit_message_text(self, chat_id, message_id, text):
        self.edit_message_text_calls.append(
            {
                "chat_id": chat_id,
                "message_id": message_id,
                "text": text,
            }
        )
        return True

    async def delete_message(self, chat_id, message_id):
        self.delete_message_calls.append({"chat_id": chat_id, "message_id": message_id})
        return True

    async def send_chat_action(self, chat_id, action="typing", message_thread_id=None):
        return True

    async def set_my_commands(self, commands):
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


class FakeCodexRunner:
    def __init__(self):
        self.calls = []
        self.next_result = CodexRunResult(
            thread_id="thread-100",
            answer="ok-answer",
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
def session_root(tmp_path: Path) -> Path:
    root = tmp_path / "sessions"
    root.mkdir(parents=True)
    path = root / "s1.jsonl"
    with path.open("w", encoding="utf-8") as f:
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
                        "message": "session title",
                    },
                },
                ensure_ascii=False,
            )
            + "\n"
        )
    return root


async def _feed(dp: Dispatcher, bot: Bot, update: dict):
    await dp.feed_raw_update(bot, update)


@pytest.mark.asyncio
async def test_help_command_via_feed_update(bot: Bot, tmp_path: Path, session_root: Path):
    api = FakeTelegramClient()
    codex = FakeCodexRunner()
    service = TgCodexService(
        api=api,
        sessions=SessionStore(session_root),
        state=StateStore(tmp_path / "state.json"),
        codex=codex,
        default_cwd=tmp_path,
        allowed_user_ids=None,
        stream_enabled=False,
        stream_edit_interval_ms=700,
        stream_min_delta_chars=8,
        thinking_status_interval_ms=900,
    )

    dp = Dispatcher()
    dp.include_router(build_router(service))

    await _feed(
        dp,
        bot,
        {
            "update_id": 1,
            "message": {
                "message_id": 11,
                "date": 1,
                "chat": {"id": 101, "type": "private"},
                "from": {"id": 1, "is_bot": False, "first_name": "u"},
                "text": "/help",
            },
        },
    )

    assert api.send_message_calls
    assert "可用命令" in api.send_message_calls[-1]["text"]


@pytest.mark.asyncio
async def test_sessions_and_callback_switch(bot: Bot, tmp_path: Path, session_root: Path):
    api = FakeTelegramClient()
    codex = FakeCodexRunner()
    state = StateStore(tmp_path / "state.json")
    service = TgCodexService(
        api=api,
        sessions=SessionStore(session_root),
        state=state,
        codex=codex,
        default_cwd=tmp_path,
        allowed_user_ids={1},
        stream_enabled=False,
        stream_edit_interval_ms=700,
        stream_min_delta_chars=8,
        thinking_status_interval_ms=900,
    )

    dp = Dispatcher()
    dp.include_router(build_router(service))

    await _feed(
        dp,
        bot,
        {
            "update_id": 2,
            "message": {
                "message_id": 12,
                "date": 1,
                "chat": {"id": 101, "type": "private"},
                "from": {"id": 1, "is_bot": False, "first_name": "u"},
                "text": "/sessions 1",
            },
        },
    )

    assert state.get_last_session_ids(1) == ["session-1"]

    await _feed(
        dp,
        bot,
        {
            "update_id": 3,
            "callback_query": {
                "id": "cq-1",
                "from": {"id": 1, "is_bot": False, "first_name": "u"},
                "chat_instance": "ci",
                "data": "use:session-1",
                "message": {
                    "message_id": 13,
                    "date": 1,
                    "chat": {"id": 101, "type": "private"},
                    "text": "x",
                },
            },
        },
    )

    active_id, _ = state.get_active(1)
    assert active_id == "session-1"
    assert api.answer_callback_query_calls


@pytest.mark.asyncio
async def test_allowlist_block(bot: Bot, tmp_path: Path, session_root: Path):
    api = FakeTelegramClient()
    codex = FakeCodexRunner()
    service = TgCodexService(
        api=api,
        sessions=SessionStore(session_root),
        state=StateStore(tmp_path / "state.json"),
        codex=codex,
        default_cwd=tmp_path,
        allowed_user_ids={1},
        stream_enabled=False,
        stream_edit_interval_ms=700,
        stream_min_delta_chars=8,
        thinking_status_interval_ms=900,
    )

    dp = Dispatcher()
    dp.include_router(build_router(service))

    await _feed(
        dp,
        bot,
        {
            "update_id": 4,
            "message": {
                "message_id": 14,
                "date": 1,
                "chat": {"id": 101, "type": "private"},
                "from": {"id": 2, "is_bot": False, "first_name": "blocked"},
                "text": "hello",
            },
        },
    )

    assert api.send_message_calls
    assert "没有权限" in api.send_message_calls[-1]["text"]
    assert codex.calls == []


@pytest.mark.asyncio
async def test_continue_and_new_session_paths(bot: Bot, tmp_path: Path, session_root: Path):
    api = FakeTelegramClient()
    codex = FakeCodexRunner()
    state = StateStore(tmp_path / "state.json")
    state.set_active_session(1, "existing-session", str(tmp_path))

    service = TgCodexService(
        api=api,
        sessions=SessionStore(session_root),
        state=state,
        codex=codex,
        default_cwd=tmp_path,
        allowed_user_ids=None,
        stream_enabled=False,
        stream_edit_interval_ms=700,
        stream_min_delta_chars=8,
        thinking_status_interval_ms=900,
    )

    dp = Dispatcher()
    dp.include_router(build_router(service))

    await _feed(
        dp,
        bot,
        {
            "update_id": 5,
            "message": {
                "message_id": 15,
                "date": 1,
                "chat": {"id": 101, "type": "private"},
                "from": {"id": 1, "is_bot": False, "first_name": "u"},
                "text": "continue prompt",
            },
        },
    )
    assert codex.calls[-1]["session_id"] == "existing-session"

    await _feed(
        dp,
        bot,
        {
            "update_id": 6,
            "message": {
                "message_id": 16,
                "date": 1,
                "chat": {"id": 101, "type": "private"},
                "from": {"id": 1, "is_bot": False, "first_name": "u"},
                "text": "/new",
            },
        },
    )

    await _feed(
        dp,
        bot,
        {
            "update_id": 7,
            "message": {
                "message_id": 17,
                "date": 1,
                "chat": {"id": 101, "type": "private"},
                "from": {"id": 1, "is_bot": False, "first_name": "u"},
                "text": "new prompt",
            },
        },
    )

    assert codex.calls[-1]["session_id"] is None
