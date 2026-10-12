"""Telegram message sending layer with formatting fallbacks and error handling.

Provides:
  - safe_reply, safe_edit, safe_send: MarkdownV2 helpers falling back to plain text
  - send_photo: sends rendered screenshots or image bytes
  - send_document: sends session artifacts
  - send_with_fallback: low-level fallback runner
"""

from __future__ import annotations

import io
import logging
from typing import Any

from telegram import Bot, LinkPreviewOptions, Message
from telegram.constants import ParseMode
from telegram.error import BadRequest, RetryAfter

from ..markdown_v2 import convert_markdown
from ..telegram_sender import (
    TELEGRAM_MAX_MESSAGE_LENGTH,
    split_markdown_for_telegram,
    utf16_len,
)

logger = logging.getLogger(__name__)

PARSE_MODE = ParseMode.MARKDOWN_V2
NO_LINK_PREVIEW = LinkPreviewOptions(is_disabled=True)


async def send_with_fallback(
    send_fn: Any,
    text: str,
    *args: Any,
    **kwargs: Any,
) -> Message:
    """Send text with MarkdownV2 formatting, falling back to plain text on error."""
    converted = convert_markdown(text)
    kwargs.setdefault("link_preview_options", NO_LINK_PREVIEW)

    # Pre-check: if rendered MarkdownV2 exceeds Telegram limit, fall back to plain text if plain text fits
    if utf16_len(converted) > TELEGRAM_MAX_MESSAGE_LENGTH:
        if utf16_len(text) <= TELEGRAM_MAX_MESSAGE_LENGTH:
            logger.warning(
                "Rendered MarkdownV2 (%d code units) exceeds limit, sending plain text (%d code units)",
                utf16_len(converted),
                utf16_len(text),
            )
            kwargs.pop("parse_mode", None)
            return await send_fn(text, *args, **kwargs)

    try:
        return await send_fn(converted, *args, parse_mode=PARSE_MODE, **kwargs)
    except BadRequest as e:
        err_str = str(e).lower()
        if "can't parse entities" in err_str or "entity" in err_str:
            logger.warning("MarkdownV2 parsing failed (%s), sending plain text", e)
            kwargs.pop("parse_mode", None)
            return await send_fn(text, *args, **kwargs)
        if "message is too long" in err_str:
            if utf16_len(text) <= TELEGRAM_MAX_MESSAGE_LENGTH:
                logger.warning(
                    "Telegram rejected MarkdownV2 as too long, sending plain text (%d code units)",
                    utf16_len(text),
                )
                kwargs.pop("parse_mode", None)
                return await send_fn(text, *args, **kwargs)
        raise


async def safe_reply(message: Any, text: str, **kwargs: Any) -> Message | None:
    """Reply to a message with formatting fallback."""
    if not message or not hasattr(message, "reply_text"):
        return None
    return await send_with_fallback(message.reply_text, text, **kwargs)


async def safe_edit(message: Any, text: str, **kwargs: Any) -> Message | None:
    """Edit message text with formatting fallback."""
    if not message or not hasattr(message, "edit_text"):
        return None
    kwargs.pop("message_thread_id", None)
    return await send_with_fallback(message.edit_text, text, **kwargs)


async def safe_send(bot: Bot, chat_id: int, text: str, **kwargs: Any) -> Message:
    """Send message to chat_id with formatting fallback."""

    async def _send(t: str, *args: Any, **kw: Any) -> Message:
        return await bot.send_message(chat_id=chat_id, text=t, *args, **kw)

    chunks = split_markdown_for_telegram(text)
    if len(chunks) > 1:
        last_msg = None
        for chunk in chunks:
            last_msg = await send_with_fallback(_send, chunk, **kwargs)
        return last_msg  # type: ignore[return-value]

    return await send_with_fallback(_send, text, **kwargs)


async def safe_edit_message(
    bot: Bot,
    chat_id: int,
    message_id: int,
    text: str,
    **kwargs: Any,
) -> Message | None:
    """Edit message text by chat_id and message_id with formatting fallback."""
    # Strip parameters not supported by Telegram edit_message_text
    kwargs.pop("message_thread_id", None)

    async def _edit(t: str, *args: Any, **kw: Any) -> Message:
        return await bot.edit_message_text(
            chat_id=chat_id, message_id=message_id, text=t, *args, **kw
        )

    try:
        return await send_with_fallback(_edit, text, **kwargs)
    except BadRequest as e:
        if "message is not modified" in str(e).lower():
            return None
        raise


async def send_photo(
    bot: Bot,
    chat_id: int,
    image_bytes: bytes,
    caption: str | None = None,
    **kwargs: Any,
) -> Message | None:
    """Send a photo to chat_id."""
    try:
        bio = io.BytesIO(image_bytes)
        bio.name = "screenshot.png"
        return await bot.send_photo(
            chat_id=chat_id,
            photo=bio,
            caption=caption,
            **kwargs,
        )
    except RetryAfter:
        raise
    except Exception as e:
        logger.error("Failed to send photo to %d: %s", chat_id, e)
        return None


async def send_document(
    bot: Bot,
    chat_id: int,
    document_data: list[tuple[str, bytes]],
    **kwargs: Any,
) -> None:
    """Send documents (e.g. artifacts) to chat_id."""
    if not document_data:
        return
    for filename, raw_bytes in document_data:
        try:
            bio = io.BytesIO(raw_bytes)
            bio.name = filename
            await bot.send_document(
                chat_id=chat_id,
                document=bio,
                filename=filename,
                **kwargs,
            )
        except RetryAfter:
            raise
        except Exception as e:
            logger.error("Failed to send document %s to %d: %s", filename, chat_id, e)
