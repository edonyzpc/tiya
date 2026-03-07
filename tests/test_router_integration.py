import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiogram import Bot, Dispatcher

from src.domain.models import AgentRunResult, ApprovalRequest, StreamConfig
from src.services.session_store import AsyncSessionStore, ClaudeSessionStore, CodexSessionStore
from src.services.state_store import StateStore
from src.services.storage import StorageManager
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
        self.edit_message_reply_markup_calls = []
        self.delete_message_calls = []
        self.answer_callback_query_calls = []
        self.send_chat_action_calls = []
        self.download_telegram_file_calls = []
        self.file_payloads = {}
        self._next_message_id = 776

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
        self._next_message_id += 1
        self.send_message_with_result_calls.append(
            {
                "chat_id": chat_id,
                "text": text,
                "reply_to": reply_to,
                "reply_markup": reply_markup,
                "parse_mode": parse_mode,
                "entities": entities,
                "message_id": self._next_message_id,
            }
        )
        return SimpleNamespace(message_id=self._next_message_id)

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
        reply_markup=None,
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
                "reply_markup": reply_markup,
                "parse_mode": parse_mode,
            }
        )
        return True

    async def edit_message_reply_markup(
        self,
        chat_id,
        message_id,
        reply_markup=None,
        fail_fast_retry_after=False,
    ):
        self.edit_message_reply_markup_calls.append(
            {
                "chat_id": chat_id,
                "message_id": message_id,
                "reply_markup": reply_markup,
            }
        )
        return True

    async def delete_message(self, chat_id, message_id):
        self.delete_message_calls.append({"chat_id": chat_id, "message_id": message_id})
        return True

    async def send_chat_action(self, chat_id, action="typing", message_thread_id=None):
        self.send_chat_action_calls.append(
            {
                "chat_id": chat_id,
                "action": action,
                "message_thread_id": message_thread_id,
            }
        )
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

    async def download_telegram_file(self, file_id, destination):
        self.download_telegram_file_calls.append({"file_id": file_id, "destination": destination})
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(self.file_payloads.get(file_id, b"fake-image"))
        return destination


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

    async def run_prompt(
        self,
        prompt,
        cwd,
        session_id=None,
        images=(),
        on_partial=None,
        on_reasoning=None,
        interaction_handler=None,
        cancel_event=None,
    ):
        self.calls.append(
            {
                "prompt": prompt,
                "cwd": str(cwd),
                "session_id": session_id,
                "images": list(images),
                "interaction_handler": interaction_handler,
                "cancel_event": cancel_event,
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
    stream_enabled: bool = False,
):
    api = FakeTelegramClient()
    codex_runner = FakeRunner("codex")
    claude_runner = FakeRunner("claude")
    codex_root, claude_root = session_roots
    storage = StorageManager(
        db_path=tmp_path / "storage" / "tiya.db",
        instance_id="router-test",
        default_provider="codex",
        attachments_root=tmp_path / "attachments",
        legacy_state_path=tmp_path / "state.json",
        session_roots={
            "codex": codex_root,
            "claude": claude_root,
        },
    )
    state = StateStore(storage, default_provider="codex", flush_delay_sec=0.01)
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
            "codex": AsyncSessionStore(CodexSessionStore(codex_root, storage)),
            "claude": AsyncSessionStore(ClaudeSessionStore(claude_root, storage)),
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
        attachments_root=tmp_path / "attachments",
        allowed_user_ids=allowed_user_ids,
        allowed_cwd_roots=allowed_cwd_roots,
        stream_config=StreamConfig(
            enabled=stream_enabled,
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


def _message_update(update_id: int, text: str, user_id: int = 1, message_id: int = 11, chat_id: int = 101) -> dict:
    return {
        "update_id": update_id,
        "message": {
            "message_id": message_id,
            "date": 1,
            "chat": {"id": chat_id, "type": "private"},
            "from": {"id": user_id, "is_bot": False, "first_name": "u"},
            "text": text,
        },
    }


def _photo_message_update(
    update_id: int,
    *,
    caption: str | None = None,
    user_id: int = 1,
    message_id: int = 11,
    chat_id: int = 101,
    media_group_id: str | None = None,
    file_id: str = "photo-file-1",
    file_unique_id: str = "photo-uniq-1",
    file_size: int = 1024,
) -> dict:
    message = {
        "message_id": message_id,
        "date": 1,
        "chat": {"id": chat_id, "type": "private"},
        "from": {"id": user_id, "is_bot": False, "first_name": "u"},
        "photo": [
            {"file_id": "photo-small", "file_unique_id": "photo-small-uniq", "width": 10, "height": 10, "file_size": 100},
            {"file_id": file_id, "file_unique_id": file_unique_id, "width": 100, "height": 100, "file_size": file_size},
        ],
    }
    if caption is not None:
        message["caption"] = caption
    if media_group_id is not None:
        message["media_group_id"] = media_group_id
    return {"update_id": update_id, "message": message}


def _document_message_update(
    update_id: int,
    *,
    caption: str | None = None,
    user_id: int = 1,
    message_id: int = 11,
    chat_id: int = 101,
    file_id: str = "doc-file-1",
    file_unique_id: str = "doc-uniq-1",
    file_name: str = "image.png",
    mime_type: str = "image/png",
    file_size: int = 1024,
) -> dict:
    message = {
        "message_id": message_id,
        "date": 1,
        "chat": {"id": chat_id, "type": "private"},
        "from": {"id": user_id, "is_bot": False, "first_name": "u"},
        "document": {
            "file_id": file_id,
            "file_unique_id": file_unique_id,
            "file_name": file_name,
            "mime_type": mime_type,
            "file_size": file_size,
        },
    }
    if caption is not None:
        message["caption"] = caption
    return {"update_id": update_id, "message": message}


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
    assert api.edit_message_reply_markup_calls
    assert api.edit_message_reply_markup_calls[-1]["message_id"] == 13

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
async def test_photo_with_caption_runs_runner_with_prompt_image(
    bot: Bot, tmp_path: Path, session_roots: tuple[Path, Path]
):
    service, api, _, codex_runner, _ = _build_service(tmp_path, session_roots)
    dp = Dispatcher()
    dp.include_router(build_router(service))

    await _feed(dp, bot, _photo_message_update(1, caption="summarize this screenshot", message_id=41))

    assert len(codex_runner.calls) == 1
    call = codex_runner.calls[-1]
    assert call["prompt"] == "summarize this screenshot"
    assert len(call["images"]) == 1
    assert call["images"][0].file_name.endswith(".jpg")
    assert api.download_telegram_file_calls
    assert not (tmp_path / "attachments" / "user-1" / "chat-101" / "msg-41").exists()
    await service.shutdown()


@pytest.mark.asyncio
async def test_photo_without_caption_waits_for_next_text_and_clears_pending(
    bot: Bot, tmp_path: Path, session_roots: tuple[Path, Path]
):
    service, api, state, codex_runner, _ = _build_service(tmp_path, session_roots)
    dp = Dispatcher()
    dp.include_router(build_router(service))

    await _feed(dp, bot, _photo_message_update(1, message_id=51))

    assert codex_runner.calls == []
    pending = await state.get_pending_image(1, provider="codex")
    assert pending is not None
    assert pending.path.exists()
    assert "下一条发送文本" in api.send_message_calls[-1]["text"]

    await _feed(dp, bot, _message_update(2, "extract the key numbers", message_id=52))

    assert len(codex_runner.calls) == 1
    call = codex_runner.calls[-1]
    assert call["prompt"] == "extract the key numbers"
    assert len(call["images"]) == 1
    assert await state.get_pending_image(1, provider="codex") is None
    assert not pending.path.parent.exists()
    await service.shutdown()


@pytest.mark.asyncio
async def test_ask_command_consumes_pending_image(
    bot: Bot, tmp_path: Path, session_roots: tuple[Path, Path]
):
    service, _, state, codex_runner, _ = _build_service(tmp_path, session_roots)
    dp = Dispatcher()
    dp.include_router(build_router(service))

    await _feed(dp, bot, _photo_message_update(1, message_id=53))
    assert await state.get_pending_image(1, provider="codex") is not None

    await _feed(dp, bot, _message_update(2, "/ask identify the creature", message_id=54))

    assert len(codex_runner.calls) == 1
    assert codex_runner.calls[-1]["prompt"] == "identify the creature"
    assert len(codex_runner.calls[-1]["images"]) == 1
    assert await state.get_pending_image(1, provider="codex") is None
    await service.shutdown()


@pytest.mark.asyncio
async def test_quick_session_pick_runs_before_pending_image_prompt(
    bot: Bot, tmp_path: Path, session_roots: tuple[Path, Path]
):
    service, api, state, codex_runner, _ = _build_service(tmp_path, session_roots)
    dp = Dispatcher()
    dp.include_router(build_router(service))

    await _feed(dp, bot, _photo_message_update(1, message_id=55))
    await _feed(dp, bot, _message_update(2, "/sessions 1", message_id=56))
    await _feed(dp, bot, _message_update(3, "1", message_id=57))

    assert codex_runner.calls == []
    assert (await state.get_active(1, provider="codex"))[0] == "session-1"
    assert await state.get_pending_image(1, provider="codex") is not None
    assert "已切换到 (codex)" in api.send_message_calls[-1]["text"]
    await service.shutdown()


@pytest.mark.asyncio
async def test_pending_image_is_provider_scoped_and_new_clears_active_provider_pending(
    bot: Bot, tmp_path: Path, session_roots: tuple[Path, Path]
):
    service, _, state, codex_runner, claude_runner = _build_service(tmp_path, session_roots)
    dp = Dispatcher()
    dp.include_router(build_router(service))

    await _feed(dp, bot, _photo_message_update(1, message_id=61))
    await _feed(dp, bot, _message_update(2, "/provider claude", message_id=62))
    await _feed(dp, bot, _photo_message_update(3, message_id=63))

    codex_pending = await state.get_pending_image(1, provider="codex")
    claude_pending = await state.get_pending_image(1, provider="claude")
    assert codex_pending is not None
    assert claude_pending is not None

    await _feed(dp, bot, _message_update(4, "/new", message_id=64))

    assert await state.get_pending_image(1, provider="claude") is None
    assert await state.get_pending_image(1, provider="codex") is not None
    assert claude_runner.calls == []

    await _feed(dp, bot, _message_update(5, "/provider codex", message_id=65))
    await _feed(dp, bot, _message_update(6, "describe the diagram", message_id=66))

    assert len(codex_runner.calls) == 1
    assert codex_runner.calls[-1]["prompt"] == "describe the diagram"
    assert len(codex_runner.calls[-1]["images"]) == 1
    await service.shutdown()


@pytest.mark.asyncio
async def test_attachment_paths_are_scoped_by_chat_id(
    bot: Bot, tmp_path: Path, session_roots: tuple[Path, Path]
):
    service, _, state, _, _ = _build_service(tmp_path, session_roots)
    dp = Dispatcher()
    dp.include_router(build_router(service))

    await _feed(dp, bot, _photo_message_update(1, message_id=11, chat_id=101))
    first_pending = await state.get_pending_image(1, provider="codex")
    assert first_pending is not None

    await _feed(dp, bot, _photo_message_update(2, message_id=11, chat_id=202))
    second_pending = await state.get_pending_image(1, provider="codex")
    assert second_pending is not None

    assert first_pending.path != second_pending.path
    assert "provider-codex" in str(first_pending.path)
    assert "provider-codex" in str(second_pending.path)
    await service.shutdown()


@pytest.mark.asyncio
async def test_invalid_image_inputs_are_rejected(bot: Bot, tmp_path: Path, session_roots: tuple[Path, Path]):
    service, api, _, codex_runner, _ = _build_service(tmp_path, session_roots)
    dp = Dispatcher()
    dp.include_router(build_router(service))

    await _feed(dp, bot, _photo_message_update(1, message_id=71, media_group_id="group-1"))
    await _feed(
        dp,
        bot,
        _document_message_update(
            2,
            message_id=72,
            file_name="notes.txt",
            mime_type="text/plain",
        ),
    )
    await _feed(
        dp,
        bot,
        _photo_message_update(3, message_id=73, file_size=21 * 1024 * 1024),
    )

    assert codex_runner.calls == []
    assert "暂不支持相册" in api.send_message_calls[-3]["text"]
    assert "只支持图片文件" in api.send_message_calls[-2]["text"]
    assert "不超过 20 MB" in api.send_message_calls[-1]["text"]
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


@pytest.mark.asyncio
async def test_request_approval_send_failure_cleans_pending_interaction(
    tmp_path: Path,
    session_roots: tuple[Path, Path],
):
    service, api, state, _, _ = _build_service(tmp_path, session_roots)
    run = await service.interactions.start_run(user_id=1, provider="codex", chat_id=101, chat_type="private")
    assert run is not None

    async def _boom(*args, **kwargs):
        raise RuntimeError("send failed")

    api.send_message_with_result = _boom  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="send failed"):
        await service._request_approval(
            chat_id=101,
            reply_to=11,
            user_id=1,
            provider="codex",
            chat_type="private",
            request=ApprovalRequest(
                kind="command",
                title="Need approval",
                body="Run a command",
                command="pwd",
            ),
        )

    assert await service.interactions.get_pending_interaction(1, "codex") is None
    assert await state.get_pending_interaction(1, provider="codex") is None
    await service.interactions.finish_run(1, "codex", run.run_id)
    await service.shutdown()


@pytest.mark.asyncio
async def test_approval_callback_binds_message_id_and_closes_interaction(
    bot: Bot,
    tmp_path: Path,
    session_roots: tuple[Path, Path],
):
    service, api, state, _, _ = _build_service(tmp_path, session_roots, allowed_user_ids={1})
    dp = Dispatcher()
    dp.include_router(build_router(service))
    run = await service.interactions.start_run(user_id=1, provider="codex", chat_id=101, chat_type="private")
    assert run is not None

    task = asyncio.create_task(
        service._request_approval(
            chat_id=101,
            reply_to=11,
            user_id=1,
            provider="codex",
            chat_type="private",
            request=ApprovalRequest(
                kind="command",
                title="Need approval",
                body="Run a command",
                command="pwd",
            ),
        )
    )

    pending = None
    for _ in range(10):
        await asyncio.sleep(0)
        pending = await state.get_pending_interaction(1, provider="codex")
        if pending is not None and pending.message_id == 777:
            break
    assert pending is not None
    assert pending.message_id == 777

    await _feed(
        dp,
        bot,
        {
            "update_id": 99,
            "callback_query": {
                "id": "cq-approval",
                "from": {"id": 1, "is_bot": False, "first_name": "u"},
                "chat_instance": "ci",
                "data": f"ixa:codex:{pending.interaction_id}:accept",
                "message": {
                    "message_id": 777,
                    "date": 1,
                    "chat": {"id": 101, "type": "private"},
                    "text": "approval message",
                },
            },
        },
    )

    assert await task == "accept"
    assert api.edit_message_reply_markup_calls
    assert api.edit_message_reply_markup_calls[-1]["message_id"] == 777
    assert api.edit_message_text_calls
    assert api.edit_message_text_calls[-1]["message_id"] == 777
    assert api.edit_message_text_calls[-1]["reply_markup"] is None
    assert "状态: 已批准一次" in api.edit_message_text_calls[-1]["text"]
    assert await state.get_pending_interaction(1, provider="codex") is None

    await service.interactions.finish_run(1, "codex", run.run_id)
    await service.shutdown()


@pytest.mark.asyncio
async def test_approval_cancel_closes_interaction_message(
    tmp_path: Path,
    session_roots: tuple[Path, Path],
):
    service, api, state, _, _ = _build_service(tmp_path, session_roots)
    run = await service.interactions.start_run(user_id=1, provider="codex", chat_id=101, chat_type="private")
    assert run is not None

    task = asyncio.create_task(
        service._request_approval(
            chat_id=101,
            reply_to=11,
            user_id=1,
            provider="codex",
            chat_type="private",
            request=ApprovalRequest(
                kind="command",
                title="Need approval",
                body="Run a command",
                command="pwd",
            ),
        )
    )

    pending = None
    for _ in range(10):
        await asyncio.sleep(0)
        pending = await state.get_pending_interaction(1, provider="codex")
        if pending is not None and pending.message_id == 777:
            break
    assert pending is not None
    assert pending.message_id == 777

    await service._handle_cancel(chat_id=101, reply_to=12, user_id=1, provider="codex")

    assert await task == "cancel"
    assert api.edit_message_text_calls
    assert api.edit_message_text_calls[-1]["message_id"] == 777
    assert "状态: 已取消" in api.edit_message_text_calls[-1]["text"]
    assert await state.get_pending_interaction(1, provider="codex") is None

    await service.interactions.finish_run(1, "codex", run.run_id)
    await service.shutdown()


@pytest.mark.asyncio
async def test_stream_preview_pauses_while_waiting_for_approval(
    bot: Bot,
    tmp_path: Path,
    session_roots: tuple[Path, Path],
):
    class ApprovalRunner(FakeRunner):
        async def run_prompt(
            self,
            prompt,
            cwd,
            session_id=None,
            images=(),
            on_partial=None,
            on_reasoning=None,
            interaction_handler=None,
            cancel_event=None,
        ):
            self.calls.append(
                {
                    "prompt": prompt,
                    "cwd": str(cwd),
                    "session_id": session_id,
                    "images": list(images),
                    "interaction_handler": interaction_handler,
                    "cancel_event": cancel_event,
                }
            )
            if on_reasoning is not None:
                await on_reasoning("planning")
            assert interaction_handler is not None
            decision = await interaction_handler.request_approval(
                ApprovalRequest(
                    kind="file_change",
                    title="Claude 请求使用 Write",
                    body="工具: Write\n文件: /tmp/test3",
                )
            )
            if decision == "accept":
                return AgentRunResult(thread_id="approval-thread", answer="ok", stderr_text="", return_code=0)
            return AgentRunResult(thread_id="approval-thread", answer="cancelled", stderr_text="", return_code=130)

    service, api, state, _, _ = _build_service(
        tmp_path,
        session_roots,
        allowed_user_ids={1},
        stream_enabled=True,
    )
    service.runners["codex"] = ApprovalRunner("codex")
    dp = Dispatcher()
    dp.include_router(build_router(service))

    task = asyncio.create_task(_feed(dp, bot, _message_update(1, "帮我在 /tmp 目录创建一个 test3", message_id=31)))
    await asyncio.sleep(0.2)

    pending = await state.get_pending_interaction(1, provider="codex")
    assert pending is not None
    assert pending.message_id == 778
    preview_edits_before_wait = len(api.edit_message_text_calls)
    typing_calls_before_wait = len(api.send_chat_action_calls)

    await asyncio.sleep(1.2)

    assert len(api.edit_message_text_calls) == preview_edits_before_wait
    assert len(api.send_chat_action_calls) == typing_calls_before_wait

    await _feed(
        dp,
        bot,
        {
            "update_id": 2,
            "callback_query": {
                "id": "cq-stream-approval",
                "from": {"id": 1, "is_bot": False, "first_name": "u"},
                "chat_instance": "ci",
                "data": f"ixa:codex:{pending.interaction_id}:accept",
                "message": {
                    "message_id": 778,
                    "date": 1,
                    "chat": {"id": 101, "type": "private"},
                    "text": "approval message",
                },
            },
        },
    )

    await task
    assert api.send_message_calls
    assert api.send_message_calls[-1]["text"] == "ok"
    await service.shutdown()
