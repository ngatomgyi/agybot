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
from pathlib import Path
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


def _clean_plaintext_fallback(text: str) -> str:
    """Clean internal formatting delimiters and convert expandable quotes to readable plain text."""
    import re
    from ..transcript_parser import TranscriptParser

    start_tag = TranscriptParser.EXPANDABLE_QUOTE_START
    end_tag = TranscriptParser.EXPANDABLE_QUOTE_END

    if start_tag in text and end_tag in text:
        def _quote_repl(m: re.Match[str]) -> str:
            inner = m.group(1).strip()
            quoted_lines = [f"> {line}" for line in inner.split("\n")]
            return "\n".join(quoted_lines)

        pattern = re.escape(start_tag) + r"([\s\S]*?)" + re.escape(end_tag)
        text = re.sub(pattern, _quote_repl, text)

    text = text.replace(start_tag, "").replace(end_tag, "")
    text = re.sub(r"\x02EXPQUOTE_\d+\x02", "", text)
    return text


async def send_with_fallback(
    send_fn: Any,
    text: str,
    *args: Any,
    **kwargs: Any,
) -> Message:
    """Send text with MarkdownV2 formatting, falling back to plain text on error."""
    converted = convert_markdown(text)
    kwargs.setdefault("link_preview_options", NO_LINK_PREVIEW)
    plain_clean = _clean_plaintext_fallback(text)

    # Pre-check: if rendered MarkdownV2 exceeds Telegram limit, fall back to plain text if plain text fits
    if utf16_len(converted) > TELEGRAM_MAX_MESSAGE_LENGTH:
        if utf16_len(plain_clean) <= TELEGRAM_MAX_MESSAGE_LENGTH:
            logger.warning(
                "Rendered MarkdownV2 (%d code units) exceeds limit, sending plain text (%d code units)",
                utf16_len(converted),
                utf16_len(plain_clean),
            )
            kwargs.pop("parse_mode", None)
            return await send_fn(plain_clean, *args, **kwargs)

    try:
        return await send_fn(converted, *args, parse_mode=PARSE_MODE, **kwargs)
    except BadRequest as e:
        err_str = str(e).lower()
        if "can't parse entities" in err_str or "entity" in err_str:
            logger.warning("MarkdownV2 parsing failed (%s), sending plain text", e)
            kwargs.pop("parse_mode", None)
            return await send_fn(plain_clean, *args, **kwargs)
        if "message is too long" in err_str:
            if utf16_len(plain_clean) <= TELEGRAM_MAX_MESSAGE_LENGTH:
                logger.warning(
                    "Telegram rejected MarkdownV2 as too long, sending plain text (%d code units)",
                    utf16_len(plain_clean),
                )
                kwargs.pop("parse_mode", None)
                return await send_fn(plain_clean, *args, **kwargs)
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
    document_data: list[tuple[str, bytes | Path | str]],
    message_thread_id: int | None = None,
    caption: str | None = None,
    **kwargs: Any,
) -> None:
    """Send documents (e.g. artifacts) to chat_id.

    Supports both standard Telegram Bot API (<= 50MB) and Telegram Developer API (up to 2GB).
    """
    if not document_data:
        return

    from ..telethon_uploader import is_telethon_available, upload_large_file

    # Extract thread ID from kwargs if not explicitly passed
    tid = message_thread_id or kwargs.pop("message_thread_id", None)

    for filename, data_or_path in document_data:
        file_path: Path | None = None
        size: int = 0

        if isinstance(data_or_path, (str, Path)):
            file_path = Path(data_or_path)
            if file_path.is_file():
                size = file_path.stat().st_size
        elif isinstance(data_or_path, (bytes, bytearray)):
            size = len(data_or_path)

        # If file is over standard 48MB limit and Developer API is available, use Developer API
        if size > 48 * 1024 * 1024 and is_telethon_available():
            target_input = file_path if file_path else data_or_path
            logger.info(
                "Sending large document %s (%.1f MB) via Developer API",
                filename,
                size / (1024 * 1024),
            )
            success = await upload_large_file(
                chat_id=chat_id,
                file_input=target_input,
                filename=filename,
                caption=caption,
                message_thread_id=tid,
            )
            if success:
                continue
            logger.warning(
                "Developer API upload failed for %s, falling back to standard API",
                filename,
            )

        # Standard Bot API upload (<= 50MB)
        try:
            if file_path:
                with open(file_path, "rb") as f:
                    await bot.send_document(
                        chat_id=chat_id,
                        document=f,
                        filename=filename,
                        caption=caption,
                        message_thread_id=tid,
                        **kwargs,
                    )
            else:
                bio = io.BytesIO(data_or_path)  # type: ignore[arg-type]
                bio.name = filename
                await bot.send_document(
                    chat_id=chat_id,
                    document=bio,
                    filename=filename,
                    caption=caption,
                    message_thread_id=tid,
                    **kwargs,
                )
        except RetryAfter:
            raise
        except Exception as e:
            # Fall back to Developer API if standard Bot API upload failed and Developer API is configured
            if is_telethon_available() and size > 0:
                logger.warning(
                    "Standard bot upload failed (%s). Retrying %s via Developer API",
                    e,
                    filename,
                )
                target_input = file_path if file_path else data_or_path
                dev_success = await upload_large_file(
                    chat_id=chat_id,
                    file_input=target_input,
                    filename=filename,
                    caption=caption,
                    message_thread_id=tid,
                )
                if dev_success:
                    continue
            logger.error("Failed to send document %s to %d: %s", filename, chat_id, e)
