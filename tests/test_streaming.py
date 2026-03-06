import asyncio
from types import SimpleNamespace

import pytest

from src.domain.models import StreamConfig
from src.telegram.rendering import RenderProfile, RenderResult, RenderedFile, RenderedPhoto, RenderedText, TelegramMessageRenderer
from src.telegram.streaming import StreamOrchestrator


class FakeRetryAfterError(RuntimeError):
    def __init__(self, retry_after: float):
        super().__init__(f"retry after {retry_after}")
        self.retry_after = retry_after


class FakeTelegramClient:
    def __init__(
        self,
        draft_fail: bool = False,
        edit_fail: bool = False,
        draft_retry_after_times: int = 0,
        edit_retry_after_times: int = 0,
        send_message_parse_fail: bool = False,
        send_document_fail: bool = False,
        send_photo_fail: bool = False,
    ):
        self.draft_fail = draft_fail
        self.edit_fail = edit_fail
        self.draft_retry_after_times = max(0, int(draft_retry_after_times))
        self.edit_retry_after_times = max(0, int(edit_retry_after_times))
        self.send_message_parse_fail = bool(send_message_parse_fail)
        self.send_document_fail = bool(send_document_fail)
        self.send_photo_fail = bool(send_photo_fail)
        self.events = []
        self.send_message_calls = []
        self.send_document_calls = []
        self.send_photo_calls = []
        self.send_message_draft_calls = []
        self.send_message_with_result_calls = []
        self.edit_message_text_calls = []
        self.delete_message_calls = []

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
        self.events.append(("send_message", chat_id, text, reply_to, parse_mode))
        self.send_message_calls.append(
            {
                "chat_id": chat_id,
                "text": text,
                "reply_to": reply_to,
                "parse_mode": parse_mode,
                "entities": entities,
                "disable_web_page_preview": disable_web_page_preview,
                "link_preview_options": link_preview_options,
            }
        )
        if self.send_message_parse_fail and (parse_mode or entities):
            raise RuntimeError("can't parse entities")

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
        self.events.append(("send_document", chat_id, file_name, reply_to))
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
        if self.send_document_fail:
            raise RuntimeError("document unavailable")
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
        self.events.append(("send_photo", chat_id, file_name, reply_to))
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
        if self.send_photo_fail:
            raise RuntimeError("photo unavailable")
        return SimpleNamespace(message_id=779)

    async def send_message_draft(
        self,
        chat_id,
        draft_id,
        text,
        message_thread_id=None,
        fail_fast_retry_after=False,
    ):
        self.events.append(("send_message_draft", chat_id, draft_id, text))
        self.send_message_draft_calls.append((chat_id, draft_id, text))
        if self.draft_retry_after_times > 0:
            self.draft_retry_after_times -= 1
            raise FakeRetryAfterError(0.1)
        if self.draft_fail:
            raise RuntimeError("draft unavailable")
        return True

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
        self.events.append(("send_message_with_result", chat_id, text, reply_to))
        self.send_message_with_result_calls.append((chat_id, text, reply_to))
        return SimpleNamespace(message_id=777)

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
        self.events.append(("edit_message_text", chat_id, message_id, text))
        self.edit_message_text_calls.append((chat_id, message_id, text))
        if self.edit_retry_after_times > 0:
            self.edit_retry_after_times -= 1
            raise FakeRetryAfterError(0.1)
        if self.edit_fail:
            raise RuntimeError("edit unavailable")
        return True

    async def delete_message(self, chat_id, message_id):
        self.events.append(("delete_message", chat_id, message_id))
        self.delete_message_calls.append((chat_id, message_id))
        return True

    async def send_chat_action(self, chat_id, action="typing", message_thread_id=None):
        self.events.append(("send_chat_action", chat_id, action))
        return True


class TestStreamOrchestrator:
    def _orchestrator(
        self,
        api: FakeTelegramClient,
        enabled: bool = True,
        retry_cooldown_ms: int = 800,
        min_delta_chars: int = 8,
        renderer: TelegramMessageRenderer | None = None,
    ) -> StreamOrchestrator:
        return StreamOrchestrator(
            api=api,
            chat_id=123,
            reply_to=999,
            stream_enabled=enabled,
            stream_config=StreamConfig(
                enabled=enabled,
                edit_interval_ms=200,
                min_delta_chars=min_delta_chars,
                thinking_status_interval_ms=5000,
                retry_cooldown_ms=retry_cooldown_ms,
                max_consecutive_preview_errors=2,
                preview_failfast=True,
            ),
            renderer=renderer,
        )

    @pytest.mark.asyncio
    async def test_draft_success_and_final_send_once(self):
        api = FakeTelegramClient(draft_fail=False)
        orchestrator = self._orchestrator(api, enabled=True)
        await orchestrator.start()
        await orchestrator.on_partial("hello world from stream")
        await asyncio.sleep(0.25)
        await orchestrator.finalize_success("final answer", reply_to=999)

        assert orchestrator.stream_mode == "draft"
        assert not orchestrator.fallback_triggered
        assert len(api.send_message_draft_calls) >= 2
        assert len(api.send_message_calls) == 1
        assert api.send_message_calls[0]["text"] == "final answer"

    @pytest.mark.asyncio
    async def test_draft_failure_triggers_edit_fallback_and_cleanup(self):
        api = FakeTelegramClient(draft_fail=True, edit_fail=False)
        orchestrator = self._orchestrator(api, enabled=True)
        await orchestrator.start()
        await orchestrator.on_partial("stream text after fallback")
        await asyncio.sleep(0.25)
        await orchestrator.finalize_success("final answer", reply_to=999)

        assert orchestrator.stream_mode == "edit_fallback"
        assert orchestrator.fallback_triggered
        assert len(api.send_message_with_result_calls) >= 1
        assert len(api.edit_message_text_calls) >= 1
        assert len(api.delete_message_calls) == 1
        assert len(api.send_message_calls) == 1

        delete_index = [i for i, evt in enumerate(api.events) if evt[0] == "delete_message"][0]
        send_index = [i for i, evt in enumerate(api.events) if evt[0] == "send_message"][0]
        assert delete_index < send_index

    @pytest.mark.asyncio
    async def test_draft_and_edit_failure_falls_back_to_typing_only(self):
        api = FakeTelegramClient(draft_fail=True, edit_fail=True)
        orchestrator = self._orchestrator(api, enabled=True)
        await orchestrator.start()
        await orchestrator.on_partial("partial output")
        await asyncio.sleep(0.1)
        await orchestrator.finalize_success("final answer", reply_to=999)

        assert orchestrator.stream_mode == "typing_only"
        assert orchestrator.fallback_triggered
        assert len(api.send_message_calls) == 1
        assert len(api.delete_message_calls) == 1

    @pytest.mark.asyncio
    async def test_stream_disabled(self):
        api = FakeTelegramClient(draft_fail=False)
        orchestrator = self._orchestrator(api, enabled=False)
        await orchestrator.start()
        await orchestrator.on_partial("ignored")
        await orchestrator.finalize_success("final answer", reply_to=999)

        assert orchestrator.stream_mode == "disabled"
        assert len(api.send_message_draft_calls) == 0
        assert len(api.edit_message_text_calls) == 0
        assert len(api.send_message_calls) == 1

    @pytest.mark.asyncio
    async def test_stream_throttle_records_drops(self):
        api = FakeTelegramClient(draft_fail=False)
        orchestrator = self._orchestrator(api, enabled=True)
        await orchestrator.start()
        await orchestrator.on_partial("x" * 40)
        await orchestrator.on_partial("x" * 52)
        await asyncio.sleep(0.05)
        await orchestrator.finalize_success("final answer", reply_to=999)

        assert orchestrator.stream_mode == "draft"
        assert orchestrator.stream_dropped_by_throttle_total >= 1

    @pytest.mark.asyncio
    async def test_reasoning_hint_updates_thinking_preview(self):
        api = FakeTelegramClient(draft_fail=False)
        orchestrator = self._orchestrator(api, enabled=True)
        await orchestrator.start()
        await orchestrator.on_reasoning("先确认用户意图，再给出简短答复")
        await asyncio.sleep(0.25)

        texts = [call[2] for call in api.send_message_draft_calls]
        assert any("先确认用户意图，再给出简短答复" in text for text in texts)

    @pytest.mark.asyncio
    async def test_retry_after_triggers_degrade_to_typing_only(self):
        api = FakeTelegramClient(draft_retry_after_times=3)
        orchestrator = self._orchestrator(api, enabled=True, retry_cooldown_ms=50)
        await orchestrator.start()
        await orchestrator.on_partial("stream text")
        await asyncio.sleep(0.55)
        await orchestrator.finalize_success("final answer", reply_to=999)

        assert orchestrator.stream_mode == "typing_only"
        assert orchestrator.retry_after_total >= 1
        assert orchestrator.preview_errors_total >= 1
        assert orchestrator.degraded_reason == "preview_retry_after_threshold"
        assert len(api.send_message_calls) == 1

    @pytest.mark.asyncio
    async def test_stream_min_delta_chars_suppresses_small_preview_updates(self):
        api = FakeTelegramClient(draft_fail=False)
        orchestrator = self._orchestrator(api, enabled=True, min_delta_chars=10)
        await orchestrator.start()
        await orchestrator.on_partial("hello world")
        await asyncio.sleep(0.25)
        first_updates = len(api.send_message_draft_calls)
        await orchestrator.on_partial("hello world!")
        await asyncio.sleep(0.25)
        await orchestrator.finalize_success("final answer", reply_to=999)

        assert len(api.send_message_draft_calls) == first_updates

    @pytest.mark.asyncio
    async def test_final_render_uses_html_parse_mode(self):
        renderer = TelegramMessageRenderer(
            enabled=True,
            final_only=True,
            style="strong",
            mode="html",
            link_preview_policy="auto",
            fail_open=True,
            backend="builtin",
        )
        api = FakeTelegramClient()
        orchestrator = self._orchestrator(api, enabled=False, renderer=renderer)
        await orchestrator.start()
        await orchestrator.finalize_success("# 标题\n\n- a\n- b", reply_to=999)

        assert len(api.send_message_calls) == 1
        assert api.send_message_calls[0]["parse_mode"] == "HTML"
        assert "<b>标题</b>" in api.send_message_calls[0]["text"]

    @pytest.mark.asyncio
    async def test_final_render_fail_open_retries_with_plain_text(self):
        renderer = TelegramMessageRenderer(
            enabled=True,
            final_only=True,
            style="strong",
            mode="html",
            link_preview_policy="auto",
            fail_open=True,
            backend="builtin",
        )
        api = FakeTelegramClient(send_message_parse_fail=True)
        orchestrator = self._orchestrator(api, enabled=False, renderer=renderer)
        await orchestrator.start()
        await orchestrator.finalize_success("```python\nprint(1)\n```", reply_to=999)

        assert len(api.send_message_calls) == 2
        assert api.send_message_calls[0]["parse_mode"] == "HTML"
        assert api.send_message_calls[1]["parse_mode"] is None

    @pytest.mark.asyncio
    async def test_final_render_telegramify_text_uses_entities(self):
        renderer = TelegramMessageRenderer(
            enabled=True,
            final_only=True,
            style="strong",
            mode="html",
            link_preview_policy="auto",
            fail_open=True,
            backend="telegramify",
        )
        api = FakeTelegramClient()
        orchestrator = self._orchestrator(api, enabled=False, renderer=renderer)
        await orchestrator.start()
        await orchestrator.finalize_success("# 标题\n\n| A | B |\n| --- | --- |\n| 1 | 2 |", reply_to=999)

        assert api.send_message_calls
        assert api.send_message_calls[0]["parse_mode"] is None
        assert api.send_message_calls[0]["entities"]

    @pytest.mark.asyncio
    async def test_final_render_sequentially_sends_text_file_and_photo(self, monkeypatch):
        renderer = TelegramMessageRenderer(
            enabled=True,
            final_only=True,
            style="strong",
            mode="html",
            link_preview_policy="auto",
            fail_open=True,
            backend="telegramify",
        )

        async def _fake_render(_: str, __: RenderProfile) -> RenderResult:
            return RenderResult(
                items=[
                    RenderedText(
                        text="intro",
                        parse_mode=None,
                        entities=None,
                        disable_web_page_preview=None,
                        fallback_text="intro",
                    ),
                    RenderedFile(
                        file_name="code.py",
                        file_data=b"print(1)\n",
                        caption_text="file caption",
                        caption_entities=None,
                        disable_web_page_preview=None,
                        fallback_text="full markdown fallback",
                    ),
                    RenderedPhoto(
                        file_name="diagram.webp",
                        file_data=b"img",
                        caption_text="photo caption",
                        caption_entities=None,
                        disable_web_page_preview=None,
                        fallback_text="full markdown fallback",
                    ),
                    RenderedText(
                        text="outro",
                        parse_mode=None,
                        entities=None,
                        disable_web_page_preview=None,
                        fallback_text="outro",
                    ),
                ],
                render_mode="telegramify",
                parse_errors=0,
            )

        monkeypatch.setattr(renderer, "render_text", _fake_render)
        api = FakeTelegramClient()
        orchestrator = self._orchestrator(api, enabled=False, renderer=renderer)
        await orchestrator.start()
        await orchestrator.finalize_success("ignored", reply_to=999)

        assert [event[0] for event in api.events] == ["send_message", "send_document", "send_photo", "send_message"]

    @pytest.mark.asyncio
    async def test_final_render_media_file_failure_falls_back_to_plain_text(self, monkeypatch):
        renderer = TelegramMessageRenderer(
            enabled=True,
            final_only=True,
            style="strong",
            mode="html",
            link_preview_policy="auto",
            fail_open=True,
            backend="telegramify",
        )

        async def _fake_render(_: str, __: RenderProfile) -> RenderResult:
            return RenderResult(
                items=[
                    RenderedText(
                        text="intro",
                        parse_mode=None,
                        entities=None,
                        disable_web_page_preview=None,
                        fallback_text="intro",
                    ),
                    RenderedFile(
                        file_name="code.py",
                        file_data=b"print(1)\n",
                        caption_text="",
                        caption_entities=None,
                        disable_web_page_preview=None,
                        fallback_text="full markdown fallback",
                    ),
                    RenderedText(
                        text="outro",
                        parse_mode=None,
                        entities=None,
                        disable_web_page_preview=None,
                        fallback_text="outro",
                    ),
                ],
                render_mode="telegramify",
                parse_errors=0,
            )

        monkeypatch.setattr(renderer, "render_text", _fake_render)
        api = FakeTelegramClient(send_document_fail=True)
        orchestrator = self._orchestrator(api, enabled=False, renderer=renderer)
        await orchestrator.start()
        await orchestrator.finalize_success("ignored", reply_to=999)

        assert len(api.send_document_calls) == 1
        assert api.send_message_calls[0]["text"] == "intro"
        assert api.send_message_calls[-1]["text"] == "print(1)\n\noutro"
        assert api.send_message_calls[-1]["parse_mode"] is None

    @pytest.mark.asyncio
    async def test_final_render_media_photo_failure_falls_back_to_plain_text(self, monkeypatch):
        renderer = TelegramMessageRenderer(
            enabled=True,
            final_only=True,
            style="strong",
            mode="html",
            link_preview_policy="auto",
            fail_open=True,
            backend="telegramify",
        )

        async def _fake_render(_: str, __: RenderProfile) -> RenderResult:
            return RenderResult(
                items=[
                    RenderedPhoto(
                        file_name="diagram.webp",
                        file_data=b"img",
                        caption_text="photo caption",
                        caption_entities=None,
                        disable_web_page_preview=None,
                        fallback_text="full markdown fallback",
                    ),
                    RenderedText(
                        text="outro",
                        parse_mode=None,
                        entities=None,
                        disable_web_page_preview=None,
                        fallback_text="outro",
                    ),
                ],
                render_mode="telegramify",
                parse_errors=0,
            )

        monkeypatch.setattr(renderer, "render_text", _fake_render)
        api = FakeTelegramClient(send_photo_fail=True)
        orchestrator = self._orchestrator(api, enabled=False, renderer=renderer)
        await orchestrator.start()
        await orchestrator.finalize_success("ignored", reply_to=999)

        assert len(api.send_photo_calls) == 1
        assert api.send_message_calls[-1]["text"] == "photo caption\n[photo upload failed: diagram.webp]\n\noutro"
