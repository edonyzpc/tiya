import asyncio
import time
from typing import Optional

from domain.models import StreamSummary
from logging_utils import log
from telegram.client import MAX_TELEGRAM_TEXT, TelegramClient


class TypingStatus:
    def __init__(
        self,
        api: TelegramClient,
        chat_id: int,
        interval_sec: float = 4.0,
        message_thread_id: Optional[int] = None,
    ):
        self.api = api
        self.chat_id = chat_id
        self.interval_sec = interval_sec
        self.message_thread_id = message_thread_id
        self._stop_event = asyncio.Event()
        self._task: Optional[asyncio.Task[None]] = None

    def start(self) -> None:
        if self._task is not None:
            return
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._stop_event.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=0.5)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._task.cancel()

    async def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                await self.api.send_chat_action(
                    self.chat_id,
                    "typing",
                    message_thread_id=self.message_thread_id,
                )
            except Exception:
                pass
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=self.interval_sec)
            except asyncio.TimeoutError:
                pass


class DraftStream:
    def __init__(
        self,
        api: TelegramClient,
        chat_id: int,
        draft_id: int,
        message_thread_id: Optional[int] = None,
        min_interval_sec: float = 0.7,
        min_delta_chars: int = 8,
    ):
        self.api = api
        self.chat_id = chat_id
        self.draft_id = max(1, int(draft_id))
        self.message_thread_id = message_thread_id
        self.min_interval_sec = max(0.2, min_interval_sec)
        self.min_delta_chars = max(1, int(min_delta_chars))
        self.enabled = True
        self.last_error: Optional[str] = None
        self.sent_updates = 0
        self.dropped_updates = 0
        self.last_push_state = "idle"
        self._last_sent_text: Optional[str] = None
        self._pending_text: Optional[str] = None
        self._last_sent_at = 0.0
        self._lock = asyncio.Lock()

    async def push(self, text: str, force: bool = False) -> bool:
        async with self._lock:
            self.last_push_state = "noop"
            if not self.enabled:
                self.last_push_state = "failed"
                return False

            clipped = self._clip_text(text)
            if not clipped:
                self.last_push_state = "skipped"
                return True
            if clipped == self._last_sent_text and not force:
                self.last_push_state = "skipped"
                return True

            now = time.monotonic()
            delta_chars = abs(len(clipped) - len(self._last_sent_text or ""))
            if (
                not force
                and (now - self._last_sent_at) < self.min_interval_sec
                and delta_chars < self.min_delta_chars
            ):
                self._pending_text = clipped
                self.dropped_updates += 1
                self.last_push_state = "throttled"
                return True
            return await self._send_locked(clipped)

    async def flush(self) -> bool:
        async with self._lock:
            if not self.enabled:
                self.last_push_state = "failed"
                return False
            if self._pending_text and self._pending_text != self._last_sent_text:
                return await self._send_locked(self._pending_text)
            self.last_push_state = "skipped"
            return True

    async def _send_locked(self, text: str) -> bool:
        try:
            await self.api.send_message_draft(
                chat_id=self.chat_id,
                draft_id=self.draft_id,
                text=text,
                message_thread_id=self.message_thread_id,
            )
        except Exception as exc:
            self.enabled = False
            self.last_error = str(exc)
            self.last_push_state = "failed"
            return False

        self._last_sent_text = text
        self._pending_text = None
        self._last_sent_at = time.monotonic()
        self.sent_updates += 1
        self.last_push_state = "sent"
        return True

    @staticmethod
    def _clip_text(text: str) -> str:
        if not text or not text.strip():
            return ""
        if len(text) <= MAX_TELEGRAM_TEXT:
            return text
        return "…" + text[-(MAX_TELEGRAM_TEXT - 1) :]


class EditFallbackStream:
    def __init__(
        self,
        api: TelegramClient,
        chat_id: int,
        message_id: int,
        min_interval_sec: float = 0.7,
        min_delta_chars: int = 8,
    ):
        self.api = api
        self.chat_id = chat_id
        self.message_id = message_id
        self.min_interval_sec = max(0.2, min_interval_sec)
        self.min_delta_chars = max(1, int(min_delta_chars))
        self.enabled = True
        self.last_error: Optional[str] = None
        self.sent_updates = 0
        self.dropped_updates = 0
        self.last_push_state = "idle"
        self._last_sent_text: Optional[str] = None
        self._pending_text: Optional[str] = None
        self._last_sent_at = 0.0
        self._lock = asyncio.Lock()

    async def push(self, text: str, force: bool = False) -> bool:
        async with self._lock:
            self.last_push_state = "noop"
            if not self.enabled:
                self.last_push_state = "failed"
                return False

            clipped = self._clip_text(text)
            if not clipped:
                self.last_push_state = "skipped"
                return True
            if clipped == self._last_sent_text and not force:
                self.last_push_state = "skipped"
                return True

            now = time.monotonic()
            delta_chars = abs(len(clipped) - len(self._last_sent_text or ""))
            if (
                not force
                and (now - self._last_sent_at) < self.min_interval_sec
                and delta_chars < self.min_delta_chars
            ):
                self._pending_text = clipped
                self.dropped_updates += 1
                self.last_push_state = "throttled"
                return True
            return await self._send_locked(clipped)

    async def flush(self) -> bool:
        async with self._lock:
            if not self.enabled:
                self.last_push_state = "failed"
                return False
            if self._pending_text and self._pending_text != self._last_sent_text:
                return await self._send_locked(self._pending_text)
            self.last_push_state = "skipped"
            return True

    async def _send_locked(self, text: str) -> bool:
        try:
            await self.api.edit_message_text(
                chat_id=self.chat_id,
                message_id=self.message_id,
                text=text,
            )
        except Exception as exc:
            self.enabled = False
            self.last_error = str(exc)
            self.last_push_state = "failed"
            return False

        self._last_sent_text = text
        self._pending_text = None
        self._last_sent_at = time.monotonic()
        self.sent_updates += 1
        self.last_push_state = "sent"
        return True

    @staticmethod
    def _clip_text(text: str) -> str:
        if not text or not text.strip():
            return ""
        if len(text) <= MAX_TELEGRAM_TEXT:
            return text
        return "…" + text[-(MAX_TELEGRAM_TEXT - 1) :]

    async def delete_preview(self) -> bool:
        try:
            await self.api.delete_message(self.chat_id, self.message_id)
            return True
        except Exception:
            return False


class StreamOrchestrator:
    def __init__(
        self,
        api: TelegramClient,
        chat_id: int,
        reply_to: int,
        stream_enabled: bool,
        stream_edit_interval_ms: int,
        stream_min_delta_chars: int,
        thinking_status_interval_ms: int,
    ):
        self.api = api
        self.chat_id = chat_id
        self.reply_to = reply_to
        self.stream_enabled = stream_enabled
        self.stream_edit_interval_ms = max(200, stream_edit_interval_ms)
        self.stream_min_delta_chars = max(1, stream_min_delta_chars)
        self.thinking_status_interval_ms = max(400, thinking_status_interval_ms)

        self.stream_mode = "typing_only" if stream_enabled else "disabled"
        self.fallback_triggered = False
        self.stream_updates_total = 0
        self.stream_dropped_by_throttle_total = 0
        self.first_token_ms = -1
        self.final_send_ms = -1
        self.state = "INIT"

        self._started_at = time.monotonic()
        self._thinking_stop = asyncio.Event()
        self._first_output = asyncio.Event()
        self._thinking_task: Optional[asyncio.Task[None]] = None
        self._stream_lock = asyncio.Lock()
        self._stream: Optional[object] = None
        self._fallback_placeholder_id: Optional[int] = None
        self._reasoning_hint: Optional[str] = None

    async def start(self) -> None:
        self.state = "THINKING"
        if not self.stream_enabled:
            return

        draft_stream = DraftStream(
            api=self.api,
            chat_id=self.chat_id,
            draft_id=self.reply_to,
            min_interval_sec=self.stream_edit_interval_ms / 1000.0,
            min_delta_chars=self.stream_min_delta_chars,
        )
        self._stream = draft_stream
        ok = await draft_stream.push("思考中...", force=True)
        self._collect_push_stats(draft_stream)
        if ok:
            self.stream_mode = "draft"
            self._start_thinking_loop()
            return

        self.fallback_triggered = True
        log(f"sendMessageDraft bootstrap failed: {draft_stream.last_error or 'unknown error'}")
        self._stream = None
        if await self._activate_edit_fallback("思考中..."):
            self._start_thinking_loop()

    async def on_partial(self, text: str) -> None:
        raw = (text or "").strip()
        if not raw:
            return
        if self.first_token_ms < 0:
            self.first_token_ms = int((time.monotonic() - self._started_at) * 1000)
        self._first_output.set()
        self.state = "STREAMING"
        preview = self._stream_preview_text(raw)
        await self._push_stream_text(preview, force=False)

    async def on_reasoning(self, summary: str) -> None:
        hint = (summary or "").strip()
        if not hint:
            return
        self._reasoning_hint = hint
        if self._first_output.is_set():
            return
        elapsed = int(time.monotonic() - self._started_at)
        await self._push_stream_text(self._thinking_status_text("思考中...", elapsed), force=True)

    async def finalize_success(self, final_text: str, reply_to: int) -> None:
        await self._finalize(final_text, reply_to=reply_to, failed=False)

    async def finalize_error(self, err_text: str, reply_to: int) -> None:
        await self._finalize(err_text, reply_to=reply_to, failed=True)

    async def stop(self) -> None:
        self._thinking_stop.set()
        if self._thinking_task is not None and self._thinking_task is not asyncio.current_task():
            try:
                await asyncio.wait_for(self._thinking_task, timeout=0.3)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._thinking_task.cancel()

    def summary(self, exit_code: int) -> StreamSummary:
        return StreamSummary(
            stream_mode=self.stream_mode,
            first_token_ms=self.first_token_ms,
            updates_total=self.stream_updates_total,
            throttled_total=self.stream_dropped_by_throttle_total,
            fallback_triggered=self.fallback_triggered,
            final_send_ms=self.final_send_ms,
            exit_code=exit_code,
        )

    def summary_line(self, exit_code: int) -> str:
        summary = self.summary(exit_code=exit_code)
        return (
            "stream summary: "
            f"stream_mode={summary.stream_mode} "
            f"first_token_ms={summary.first_token_ms} "
            f"stream_updates_total={summary.updates_total} "
            f"stream_dropped_by_throttle_total={summary.throttled_total} "
            f"fallback_triggered={str(summary.fallback_triggered).lower()} "
            f"final_send_ms={summary.final_send_ms} "
            f"exit_code={summary.exit_code}"
        )

    @staticmethod
    def _stream_preview_text(text: str) -> str:
        raw = text.strip() or "..."
        suffix = "\n\n[生成中...]"
        max_size = min(3800, MAX_TELEGRAM_TEXT)
        if len(raw) + len(suffix) <= max_size:
            return raw + suffix
        keep = max_size - len(suffix) - 1
        if keep <= 0:
            return raw[:max_size]
        return raw[:keep] + "…" + suffix

    def _start_thinking_loop(self) -> None:
        if self._thinking_task is not None:
            return
        self._thinking_task = asyncio.create_task(self._thinking_loop())

    def _thinking_status_text(self, phase: str, elapsed_sec: int) -> str:
        hint = (self._reasoning_hint or "").strip()
        if hint:
            return f"{phase}\n{hint}\n\n已等待 {elapsed_sec}s"
        return f"{phase}\n\n已等待 {elapsed_sec}s"

    async def _thinking_loop(self) -> None:
        phases = ["思考中", "思考中.", "思考中..", "思考中..."]
        idx = 0
        while not self._thinking_stop.is_set():
            try:
                await asyncio.wait_for(
                    self._thinking_stop.wait(),
                    timeout=self.thinking_status_interval_ms / 1000.0,
                )
                return
            except asyncio.TimeoutError:
                pass
            if self._first_output.is_set():
                return
            elapsed = int(time.monotonic() - self._started_at)
            status_text = self._thinking_status_text(phases[idx % len(phases)], elapsed)
            idx += 1
            await self._push_stream_text(status_text, force=True)

    def _collect_push_stats(self, stream: object) -> None:
        state = getattr(stream, "last_push_state", "")
        if state == "sent":
            self.stream_updates_total += 1
        elif state == "throttled":
            self.stream_dropped_by_throttle_total += 1

    async def _activate_edit_fallback(self, initial_text: str) -> bool:
        self.fallback_triggered = True
        try:
            sent = await self.api.send_message_with_result(
                chat_id=self.chat_id,
                text=initial_text,
                reply_to=self.reply_to,
            )
        except Exception as exc:
            log(f"stream fallback bootstrap failed: {exc}")
            self.stream_mode = "typing_only"
            self._fallback_placeholder_id = None
            return False

        message_id = getattr(sent, "message_id", None)
        if not isinstance(message_id, int):
            log("stream fallback bootstrap failed: missing message_id")
            self.stream_mode = "typing_only"
            self._fallback_placeholder_id = None
            return False

        self._fallback_placeholder_id = message_id
        stream = EditFallbackStream(
            api=self.api,
            chat_id=self.chat_id,
            message_id=message_id,
            min_interval_sec=self.stream_edit_interval_ms / 1000.0,
            min_delta_chars=self.stream_min_delta_chars,
        )
        self._stream = stream
        self.stream_mode = "edit_fallback"
        ok = await stream.push(initial_text, force=True)
        self._collect_push_stats(stream)
        if not ok:
            log(f"stream fallback first push failed: {stream.last_error or 'unknown error'}")
            self._stream = None
            self.stream_mode = "typing_only"
            return False
        return True

    async def _push_stream_text(self, text: str, force: bool) -> None:
        needs_fallback = False
        stream: Optional[object] = None

        async with self._stream_lock:
            stream = self._stream
            if stream is None:
                return
            ok = bool(await getattr(stream, "push")(text, force=force))
            self._collect_push_stats(stream)
            if ok:
                return
            if isinstance(stream, DraftStream):
                self._stream = None
                needs_fallback = True
            else:
                log(f"stream edit fallback failed: {getattr(stream, 'last_error', 'unknown error')}")
                self._stream = None
                self.stream_mode = "typing_only"

        if needs_fallback:
            log(f"sendMessageDraft runtime failed: {getattr(stream, 'last_error', 'unknown error')}")
            await self._activate_edit_fallback(text)

    async def _cleanup_fallback_preview(self) -> None:
        placeholder_id = self._fallback_placeholder_id
        if placeholder_id is None:
            return
        try:
            await self.api.delete_message(self.chat_id, placeholder_id)
        except Exception as exc:
            log(f"cleanup fallback preview failed: {exc}")
        self._fallback_placeholder_id = None

    async def _finalize(self, text: str, reply_to: int, failed: bool) -> None:
        if self.state in ("DONE", "FAILED"):
            return
        self.state = "FINALIZING"
        await self.stop()
        await self._cleanup_fallback_preview()

        final_text = (text or "").strip() or "Codex 没有返回可展示内容。"
        send_started = time.monotonic()
        await self.api.send_message(self.chat_id, final_text, reply_to=reply_to)
        self.final_send_ms = int((time.monotonic() - send_started) * 1000)
        self.state = "FAILED" if failed else "DONE"
