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


def _is_retry_after(exc: Optional[BaseException]) -> bool:
    if exc is None:
        return False
    if getattr(exc, "retry_after", None) is not None:
        return True
    return "retryafter" in exc.__class__.__name__.lower()


def _retry_after_seconds(exc: Optional[BaseException]) -> float:
    if exc is None:
        return 0.0
    value = getattr(exc, "retry_after", None)
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return 0.0


class DraftStream:
    def __init__(
        self,
        api: TelegramClient,
        chat_id: int,
        draft_id: int,
        message_thread_id: Optional[int] = None,
        min_interval_sec: float = 0.7,
        min_delta_chars: int = 8,
        fail_fast_retry_after: bool = False,
    ):
        self.api = api
        self.chat_id = chat_id
        self.draft_id = max(1, int(draft_id))
        self.message_thread_id = message_thread_id
        self.min_interval_sec = max(0.2, min_interval_sec)
        self.min_delta_chars = max(1, int(min_delta_chars))
        self.fail_fast_retry_after = bool(fail_fast_retry_after)
        self.enabled = True
        self.last_error: Optional[str] = None
        self.last_exception: Optional[BaseException] = None
        self.last_error_kind = ""
        self.sent_updates = 0
        self.dropped_updates = 0
        self.last_push_state = "idle"
        self._last_sent_text: Optional[str] = None
        self._lock = asyncio.Lock()

    async def push(self, text: str) -> bool:
        async with self._lock:
            self.last_push_state = "noop"
            if not self.enabled:
                self.last_push_state = "failed"
                return False

            clipped = self._clip_text(text)
            if not clipped:
                self.last_push_state = "skipped"
                return True
            if clipped == self._last_sent_text:
                self.last_push_state = "skipped"
                return True

            try:
                await self.api.send_message_draft(
                    chat_id=self.chat_id,
                    draft_id=self.draft_id,
                    text=clipped,
                    message_thread_id=self.message_thread_id,
                    fail_fast_retry_after=self.fail_fast_retry_after,
                )
            except Exception as exc:
                self.last_exception = exc
                self.last_error = str(exc)
                if _is_retry_after(exc):
                    self.last_error_kind = "retry_after"
                    self.last_push_state = "failed_retry_after"
                else:
                    self.last_error_kind = "error"
                    self.enabled = False
                    self.last_push_state = "failed"
                return False

            self.last_exception = None
            self.last_error = None
            self.last_error_kind = ""
            self._last_sent_text = clipped
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
        fail_fast_retry_after: bool = False,
    ):
        self.api = api
        self.chat_id = chat_id
        self.message_id = message_id
        self.min_interval_sec = max(0.2, min_interval_sec)
        self.min_delta_chars = max(1, int(min_delta_chars))
        self.fail_fast_retry_after = bool(fail_fast_retry_after)
        self.enabled = True
        self.last_error: Optional[str] = None
        self.last_exception: Optional[BaseException] = None
        self.last_error_kind = ""
        self.sent_updates = 0
        self.dropped_updates = 0
        self.last_push_state = "idle"
        self._last_sent_text: Optional[str] = None
        self._lock = asyncio.Lock()

    async def push(self, text: str) -> bool:
        async with self._lock:
            self.last_push_state = "noop"
            if not self.enabled:
                self.last_push_state = "failed"
                return False

            clipped = self._clip_text(text)
            if not clipped:
                self.last_push_state = "skipped"
                return True
            if clipped == self._last_sent_text:
                self.last_push_state = "skipped"
                return True

            try:
                await self.api.edit_message_text(
                    chat_id=self.chat_id,
                    message_id=self.message_id,
                    text=clipped,
                    fail_fast_retry_after=self.fail_fast_retry_after,
                )
            except Exception as exc:
                self.last_exception = exc
                self.last_error = str(exc)
                if _is_retry_after(exc):
                    self.last_error_kind = "retry_after"
                    self.last_push_state = "failed_retry_after"
                else:
                    self.last_error_kind = "error"
                    self.enabled = False
                    self.last_push_state = "failed"
                return False

            self.last_exception = None
            self.last_error = None
            self.last_error_kind = ""
            self._last_sent_text = clipped
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
        retry_cooldown_ms: int = 15000,
        max_consecutive_preview_errors: int = 2,
        preview_failfast: bool = True,
    ):
        self.api = api
        self.chat_id = chat_id
        self.reply_to = reply_to
        self.stream_enabled = stream_enabled
        self.stream_edit_interval_ms = max(200, stream_edit_interval_ms)
        self.stream_min_delta_chars = max(1, stream_min_delta_chars)
        self.thinking_status_interval_ms = max(400, thinking_status_interval_ms)
        self.retry_cooldown_ms = max(0, retry_cooldown_ms)
        self.max_consecutive_preview_errors = max(1, max_consecutive_preview_errors)
        self.preview_failfast = bool(preview_failfast)

        self.stream_mode = "typing_only" if stream_enabled else "disabled"
        self.fallback_triggered = False
        self.stream_updates_total = 0
        self.stream_dropped_by_throttle_total = 0
        self.preview_errors_total = 0
        self.retry_after_total = 0
        self.degraded_reason = ""
        self.degraded_at_ms = -1
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

        self._sender_stop = asyncio.Event()
        self._sender_wakeup = asyncio.Event()
        self._sender_task: Optional[asyncio.Task[None]] = None
        self._latest_preview_text = ""
        self._preview_dirty = False
        self._last_preview_sent_text = ""
        self._next_preview_send_at = 0.0
        self._preview_cooldown_until = 0.0
        self._base_preview_interval_sec = self.stream_edit_interval_ms / 1000.0
        self._adaptive_preview_interval_sec = self._base_preview_interval_sec
        self._consecutive_preview_errors = 0

    async def start(self) -> None:
        self.state = "THINKING"
        if not self.stream_enabled:
            return

        initial = self._thinking_status_text(0, self._thinking_marquee_frame(0))
        draft_stream = DraftStream(
            api=self.api,
            chat_id=self.chat_id,
            draft_id=self.reply_to,
            min_interval_sec=self.stream_edit_interval_ms / 1000.0,
            min_delta_chars=self.stream_min_delta_chars,
            fail_fast_retry_after=self.preview_failfast,
        )
        self._stream = draft_stream
        self.stream_mode = "draft"
        ok = await draft_stream.push(initial)
        self._collect_push_stats(draft_stream)
        if ok:
            self._last_preview_sent_text = initial
        else:
            self.preview_errors_total += 1
            if draft_stream.last_error_kind == "retry_after":
                await self._on_preview_error(draft_stream, "draft_retry_after_bootstrap")
            else:
                self.fallback_triggered = True
                log(f"sendMessageDraft bootstrap failed: {draft_stream.last_error or 'unknown error'}")
                self._stream = None
                if not await self._activate_edit_fallback(initial):
                    await self._degrade_preview("draft_bootstrap_failed")

        if self._stream is not None:
            self._latest_preview_text = initial
            self._start_sender_loop()
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
        self._enqueue_preview_text(preview)

    async def on_reasoning(self, summary: str) -> None:
        hint = (summary or "").strip()
        if not hint:
            return
        self._reasoning_hint = hint
        if self._first_output.is_set():
            return
        elapsed = int(time.monotonic() - self._started_at)
        self._enqueue_preview_text(self._thinking_status_text(elapsed, self._thinking_marquee_frame(0)))

    async def finalize_success(self, final_text: str, reply_to: int) -> None:
        await self._finalize(final_text, reply_to=reply_to, failed=False)

    async def finalize_error(self, err_text: str, reply_to: int) -> None:
        await self._finalize(err_text, reply_to=reply_to, failed=True)

    async def stop(self) -> None:
        self._thinking_stop.set()
        self._sender_stop.set()
        self._sender_wakeup.set()
        if self._thinking_task is not None and self._thinking_task is not asyncio.current_task():
            try:
                await asyncio.wait_for(self._thinking_task, timeout=0.3)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._thinking_task.cancel()
        if self._sender_task is not None and self._sender_task is not asyncio.current_task():
            try:
                await asyncio.wait_for(self._sender_task, timeout=0.3)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._sender_task.cancel()

    def summary(self, exit_code: int) -> StreamSummary:
        return StreamSummary(
            stream_mode=self.stream_mode,
            first_token_ms=self.first_token_ms,
            updates_total=self.stream_updates_total,
            throttled_total=self.stream_dropped_by_throttle_total,
            fallback_triggered=self.fallback_triggered,
            preview_errors_total=self.preview_errors_total,
            retry_after_total=self.retry_after_total,
            degraded_reason=self.degraded_reason,
            degraded_at_ms=self.degraded_at_ms,
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
            f"preview_errors_total={summary.preview_errors_total} "
            f"retry_after_total={summary.retry_after_total} "
            f"degraded_reason={summary.degraded_reason or '-'} "
            f"degraded_at_ms={summary.degraded_at_ms} "
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

    def _start_sender_loop(self) -> None:
        if self._sender_task is not None:
            return
        self._sender_task = asyncio.create_task(self._sender_loop())

    def _start_thinking_loop(self) -> None:
        if self._thinking_task is not None:
            return
        self._thinking_task = asyncio.create_task(self._thinking_loop())

    @staticmethod
    def _thinking_marquee_frame(idx: int) -> str:
        frames = ["[>    ]", "[>>   ]", "[ >>> ]", "[  >>>]", "[   >>]", "[    >]", "[   >>]", "[  >>>]"]
        return frames[idx % len(frames)]

    def _thinking_status_text(self, elapsed_sec: int, marquee: str) -> str:
        header = f"{marquee} 思考中[{elapsed_sec}s]..."
        hint = (self._reasoning_hint or "").strip()
        if hint:
            return f"{header}\n{hint}"
        return header

    async def _thinking_loop(self) -> None:
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
            status_text = self._thinking_status_text(elapsed, self._thinking_marquee_frame(idx))
            idx += 1
            self._enqueue_preview_text(status_text)

    async def _sender_loop(self) -> None:
        while not self._sender_stop.is_set():
            if self._stream is None or not self._preview_dirty:
                try:
                    await asyncio.wait_for(self._sender_wakeup.wait(), timeout=0.2)
                except asyncio.TimeoutError:
                    pass
                self._sender_wakeup.clear()
                continue

            now = time.monotonic()
            due = max(self._next_preview_send_at, self._preview_cooldown_until)
            if now < due:
                try:
                    await asyncio.wait_for(self._sender_wakeup.wait(), timeout=min(0.2, due - now))
                except asyncio.TimeoutError:
                    pass
                self._sender_wakeup.clear()
                continue

            self._sender_wakeup.clear()
            text = (self._latest_preview_text or "").strip()
            if not text:
                self._preview_dirty = False
                continue
            stream = self._stream
            if stream is None:
                continue

            ok = bool(await getattr(stream, "push")(text))
            self._collect_push_stats(stream)
            if ok:
                self._consecutive_preview_errors = 0
                self._last_preview_sent_text = text
                self._preview_dirty = self._latest_preview_text != text
                self._next_preview_send_at = time.monotonic() + self._adaptive_preview_interval_sec
                if time.monotonic() >= self._preview_cooldown_until:
                    self._adaptive_preview_interval_sec = self._base_preview_interval_sec
                continue

            self.preview_errors_total += 1
            await self._on_preview_error(stream, "runtime")
            self._preview_dirty = self._stream is not None

    def _collect_push_stats(self, stream: object) -> None:
        state = getattr(stream, "last_push_state", "")
        if state == "sent":
            self.stream_updates_total += 1
        elif state == "throttled":
            self.stream_dropped_by_throttle_total += 1

    async def _on_preview_error(self, stream: object, phase: str) -> None:
        error_kind = getattr(stream, "last_error_kind", "")
        error_text = getattr(stream, "last_error", "unknown error")
        error_exc = getattr(stream, "last_exception", None)
        self._consecutive_preview_errors += 1

        if error_kind == "retry_after":
            self.retry_after_total += 1
            retry_after_sec = _retry_after_seconds(error_exc)
            cooldown_sec = max(self.retry_cooldown_ms / 1000.0, retry_after_sec)
            self._preview_cooldown_until = time.monotonic() + cooldown_sec
            self._adaptive_preview_interval_sec = min(
                5.0,
                max(self._base_preview_interval_sec, self._adaptive_preview_interval_sec * 1.8),
            )
            self._next_preview_send_at = self._preview_cooldown_until
            if self._consecutive_preview_errors >= self.max_consecutive_preview_errors:
                await self._degrade_preview("preview_retry_after_threshold")
            return

        if isinstance(stream, DraftStream):
            if phase == "runtime":
                log(f"sendMessageDraft runtime failed: {error_text or 'unknown error'}")
            self._stream = None
            self.fallback_triggered = True
            if await self._activate_edit_fallback(self._latest_preview_text):
                self._consecutive_preview_errors = 0
                self._next_preview_send_at = time.monotonic()
                self._preview_cooldown_until = 0.0
                self._adaptive_preview_interval_sec = self._base_preview_interval_sec
                return
            await self._degrade_preview("draft_preview_failed")
            return

        if isinstance(stream, EditFallbackStream):
            log(f"stream edit fallback failed: {error_text or 'unknown error'}")
            if self._consecutive_preview_errors >= self.max_consecutive_preview_errors:
                await self._degrade_preview("edit_preview_failed")
            return

        await self._degrade_preview("preview_stream_unknown_error")

    def _enqueue_preview_text(self, text: str) -> None:
        if self._stream is None:
            return
        preview = (text or "").strip()
        if not preview:
            return
        if self._preview_dirty and self._latest_preview_text and self._latest_preview_text != preview:
            self.stream_dropped_by_throttle_total += 1
        self._latest_preview_text = preview
        self._preview_dirty = True
        self._sender_wakeup.set()

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
            fail_fast_retry_after=self.preview_failfast,
        )
        self._stream = stream
        self.stream_mode = "edit_fallback"
        ok = await stream.push(initial_text)
        self._collect_push_stats(stream)
        if ok:
            self._last_preview_sent_text = initial_text
            return True

        self.preview_errors_total += 1
        if stream.last_error_kind == "retry_after":
            await self._on_preview_error(stream, "fallback_bootstrap_retry_after")
            return self.stream_mode != "typing_only"

        log(f"stream fallback first push failed: {stream.last_error or 'unknown error'}")
        self._stream = None
        self.stream_mode = "typing_only"
        return False

    async def _degrade_preview(self, reason: str) -> None:
        if self.stream_mode == "typing_only":
            return
        self.stream_mode = "typing_only"
        self.degraded_reason = reason
        self.degraded_at_ms = int((time.monotonic() - self._started_at) * 1000)
        self._stream = None
        self._preview_dirty = False
        self._latest_preview_text = ""
        self._sender_wakeup.set()
        log(
            "stream degrade reason="
            f"{reason} consecutive_preview_errors={self._consecutive_preview_errors} "
            f"retry_after_total={self.retry_after_total} preview_errors_total={self.preview_errors_total}"
        )
        await self._cleanup_fallback_preview()

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
