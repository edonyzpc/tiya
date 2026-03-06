from dataclasses import dataclass
from typing import Any, Optional

from ..logging_utils import log
from .client import TelegramClient
from .rendering import RenderResult, RenderedFile, RenderedPhoto, RenderedText


@dataclass(frozen=True)
class DispatchStats:
    total_items: int
    fallback_items: int
    media_fallback_used: bool


async def send_render_result(
    api: TelegramClient,
    chat_id: int,
    render_result: RenderResult,
    *,
    reply_to: Optional[int] = None,
    reply_markup: Optional[Any] = None,
    fail_open: bool,
    log_prefix: str,
) -> DispatchStats:
    fallback_items = 0
    media_fallback_used = False
    total_items = len(render_result.items)

    for idx, item in enumerate(render_result.items):
        attach_reply_to = reply_to if idx == 0 else None
        attach_markup = reply_markup if idx == total_items - 1 else None
        try:
            if isinstance(item, RenderedText):
                await api.send_message(
                    chat_id=chat_id,
                    text=item.text,
                    reply_to=attach_reply_to,
                    reply_markup=attach_markup,
                    parse_mode=item.parse_mode,
                    entities=item.entities,
                    disable_web_page_preview=item.disable_web_page_preview,
                )
                continue

            if isinstance(item, RenderedFile):
                await api.send_document(
                    chat_id=chat_id,
                    file_name=item.file_name,
                    file_data=item.file_data,
                    caption_text=item.caption_text or None,
                    caption_entities=item.caption_entities,
                    reply_to=attach_reply_to,
                    reply_markup=attach_markup,
                )
                continue

            await api.send_photo(
                chat_id=chat_id,
                file_name=item.file_name,
                file_data=item.file_data,
                caption_text=item.caption_text or None,
                caption_entities=item.caption_entities,
                reply_to=attach_reply_to,
                reply_markup=attach_markup,
            )
        except Exception as exc:
            fallback_items += 1
            if not fail_open:
                raise

            if isinstance(item, RenderedText):
                log(f"{log_prefix} text fallback: err={exc}")
                await api.send_message(
                    chat_id=chat_id,
                    text=item.fallback_text,
                    reply_to=attach_reply_to,
                    reply_markup=attach_markup,
                    disable_web_page_preview=item.disable_web_page_preview,
                )
                continue

            media_fallback_used = True
            kind = "file" if isinstance(item, RenderedFile) else "photo"
            log(f"{log_prefix} media fallback: kind={kind} err={exc}")
            await api.send_message(
                chat_id=chat_id,
                text=item.fallback_text,
                reply_to=attach_reply_to,
                reply_markup=attach_markup,
                disable_web_page_preview=item.disable_web_page_preview,
            )
            break

    return DispatchStats(
        total_items=total_items,
        fallback_items=fallback_items,
        media_fallback_used=media_fallback_used,
    )
