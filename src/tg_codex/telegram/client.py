import asyncio
import time
from typing import Any, Awaitable, Callable, Optional

from aiogram import Bot
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramNetworkError,
    TelegramRetryAfter,
    TelegramServerError,
)
from aiogram.types import BotCommand, InlineKeyboardMarkup, MenuButtonCommands, Message

from tg_codex.logging_utils import log

MAX_TELEGRAM_TEXT = 4096


class TelegramClient:
    def __init__(
        self,
        bot: Bot,
        request_max_retries: int = 2,
        request_retry_base_ms: int = 300,
        request_retry_max_ms: int = 3000,
    ):
        self.bot = bot
        self.request_max_retries = max(0, int(request_max_retries))
        self.request_retry_base_ms = max(0, int(request_retry_base_ms))
        self.request_retry_max_ms = max(self.request_retry_base_ms, int(request_retry_max_ms))

    async def _call_with_retries(self, method: str, call: Callable[[], Awaitable[Any]]) -> Any:
        total_attempts = self.request_max_retries + 1
        for attempt in range(1, total_attempts + 1):
            try:
                return await call()
            except TelegramRetryAfter as exc:
                if attempt >= total_attempts:
                    raise
                wait_sec = max(0.0, float(exc.retry_after))
                wait_ms = int(wait_sec * 1000)
                log(
                    "telegram request retry_after: "
                    f"method={method} attempt={attempt}/{total_attempts} wait_ms={wait_ms}"
                )
                await asyncio.sleep(wait_sec)
            except (TelegramNetworkError, TelegramServerError) as exc:
                if attempt >= total_attempts:
                    raise
                delay_ms = min(
                    self.request_retry_max_ms,
                    self.request_retry_base_ms * (2 ** (attempt - 1)),
                )
                log(
                    "telegram request retry: "
                    f"method={method} attempt={attempt}/{total_attempts} "
                    f"wait_ms={delay_ms} err={exc}"
                )
                if delay_ms > 0:
                    await asyncio.sleep(delay_ms / 1000.0)
            except TelegramBadRequest:
                raise

        raise RuntimeError(f"telegram request failed without explicit error: method={method}")

    @staticmethod
    def chunk_text(text: str, size: int = 3800) -> list[str]:
        if len(text) <= size:
            return [text]
        chunks: list[str] = []
        start = 0
        while start < len(text):
            end = min(start + size, len(text))
            if end < len(text):
                split_at = text.rfind("\n", start, end)
                if split_at > start:
                    end = split_at + 1
            chunks.append(text[start:end])
            start = end
        return chunks

    @staticmethod
    def _normalize_markup(reply_markup: Optional[Any]) -> Optional[Any]:
        if reply_markup is None:
            return None
        if isinstance(reply_markup, dict):
            return InlineKeyboardMarkup.model_validate(reply_markup)
        return reply_markup

    async def send_message(
        self,
        chat_id: int,
        text: str,
        reply_to: Optional[int] = None,
        reply_markup: Optional[Any] = None,
        message_thread_id: Optional[int] = None,
    ) -> None:
        for part in self.chunk_text(text, size=min(3800, MAX_TELEGRAM_TEXT)):
            await self.send_message_with_result(
                chat_id=chat_id,
                text=part,
                reply_to=reply_to,
                reply_markup=reply_markup,
                message_thread_id=message_thread_id,
            )

    async def send_message_with_result(
        self,
        chat_id: int,
        text: str,
        reply_to: Optional[int] = None,
        reply_markup: Optional[Any] = None,
        message_thread_id: Optional[int] = None,
    ) -> Message:
        markup = self._normalize_markup(reply_markup)

        async def _call() -> Message:
            return await self.bot.send_message(
                chat_id=chat_id,
                text=text,
                reply_to_message_id=reply_to,
                reply_markup=markup,
                message_thread_id=message_thread_id,
            )

        return await self._call_with_retries("sendMessage", _call)

    async def send_message_draft(
        self,
        chat_id: int,
        draft_id: int,
        text: str,
        message_thread_id: Optional[int] = None,
    ) -> bool:
        async def _call() -> bool:
            return await self.bot.send_message_draft(
                chat_id=chat_id,
                draft_id=draft_id,
                text=text,
                message_thread_id=message_thread_id,
            )

        return bool(await self._call_with_retries("sendMessageDraft", _call))

    async def edit_message_text(self, chat_id: int, message_id: int, text: str) -> Message:
        async def _call() -> Message:
            return await self.bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=text,
            )

        return await self._call_with_retries("editMessageText", _call)

    async def delete_message(self, chat_id: int, message_id: int) -> bool:
        async def _call() -> bool:
            return await self.bot.delete_message(chat_id=chat_id, message_id=message_id)

        return bool(await self._call_with_retries("deleteMessage", _call))

    async def send_chat_action(
        self,
        chat_id: int,
        action: str = "typing",
        message_thread_id: Optional[int] = None,
    ) -> bool:
        async def _call() -> bool:
            return await self.bot.send_chat_action(
                chat_id=chat_id,
                action=action,
                message_thread_id=message_thread_id,
            )

        return bool(await self._call_with_retries("sendChatAction", _call))

    async def set_my_commands(self, commands: list[dict[str, str]]) -> bool:
        telegram_commands = [BotCommand(command=item["command"], description=item["description"]) for item in commands]

        async def _call() -> bool:
            return await self.bot.set_my_commands(telegram_commands)

        return bool(await self._call_with_retries("setMyCommands", _call))

    async def set_chat_menu_button_commands(self) -> bool:
        async def _call() -> bool:
            return await self.bot.set_chat_menu_button(menu_button=MenuButtonCommands())

        return bool(await self._call_with_retries("setChatMenuButton", _call))

    async def answer_callback_query(
        self,
        callback_query_id: str,
        text: Optional[str] = None,
        show_alert: bool = False,
    ) -> bool:
        async def _call() -> bool:
            return await self.bot.answer_callback_query(
                callback_query_id=callback_query_id,
                text=text,
                show_alert=show_alert,
            )

        return bool(await self._call_with_retries("answerCallbackQuery", _call))


def monotonic_ms(started_at: float) -> int:
    return int((time.monotonic() - started_at) * 1000)
