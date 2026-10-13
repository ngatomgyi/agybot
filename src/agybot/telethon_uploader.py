"""Telegram Developer API (MTProto) uploader for large files up to 2GB.

Allows sending build artifacts, binaries, and large documents exceeding
Telegram's standard 50MB Bot API HTTP limit.
"""

from __future__ import annotations

import asyncio
import io
import logging
from pathlib import Path
from typing import Any

from telethon import TelegramClient
from telethon.tl.types import DocumentAttributeFilename

from .config import config

logger = logging.getLogger(__name__)

_telethon_client: TelegramClient | None = None
_client_lock = asyncio.Lock()


def is_telethon_available() -> bool:
    """Check if Telegram Developer API credentials (API ID & Hash) are configured."""
    return bool(config.telegram_api_id and config.telegram_api_hash)


async def get_telethon_client() -> TelegramClient | None:
    """Get or lazily initialize the Telethon client singleton."""
    global _telethon_client
    if not is_telethon_available():
        return None

    if _telethon_client is not None and _telethon_client.is_connected():
        return _telethon_client

    async with _client_lock:
        if _telethon_client is not None and _telethon_client.is_connected():
            return _telethon_client

        session_path = str(config.config_dir / "telethon.session")
        try:
            client = TelegramClient(
                session_path,
                config.telegram_api_id,
                config.telegram_api_hash,
            )
            await client.start(bot_token=config.telegram_bot_token)
            _telethon_client = client
            logger.info(
                "Telegram Developer API client connected successfully (2GB upload enabled)"
            )
            return _telethon_client
        except Exception as e:
            logger.error("Failed to initialize Telegram Developer API client: %s", e)
            return None


async def close_telethon_client() -> None:
    """Close Telethon client on application shutdown."""
    global _telethon_client
    async with _client_lock:
        if _telethon_client is not None:
            try:
                await _telethon_client.disconnect()
            except Exception as e:
                logger.debug("Error disconnecting Telethon client: %s", e)
            _telethon_client = None


async def upload_large_file(
    chat_id: int,
    file_input: str | Path | bytes | io.BytesIO,
    filename: str | None = None,
    caption: str | None = None,
    message_thread_id: int | None = None,
    progress_callback: Any = None,
) -> bool:
    """Upload large file up to 2GB using MTProto client.

    Args:
        chat_id: Destination chat/group ID.
        file_input: Path to file, raw bytes, or BytesIO buffer.
        filename: Optional override for document filename.
        caption: Optional caption text.
        message_thread_id: Optional Telegram forum topic thread ID.
        progress_callback: Optional progress callback (current, total).

    Returns:
        True if sent successfully, False otherwise.
    """
    client = await get_telethon_client()
    if client is None:
        return False

    attributes = []
    file_to_send: Any = None

    if isinstance(file_input, (str, Path)):
        p = Path(file_input)
        if not p.is_file():
            logger.error("File not found for Developer API upload: %s", file_input)
            return False
        file_to_send = str(p)
        target_name = filename or p.name
        attributes.append(DocumentAttributeFilename(file_name=target_name))
    elif isinstance(file_input, (bytes, bytearray)):
        bio = io.BytesIO(file_input)
        target_name = filename or "document"
        bio.name = target_name
        file_to_send = bio
        attributes.append(DocumentAttributeFilename(file_name=target_name))
    elif isinstance(file_input, io.BytesIO):
        target_name = filename or getattr(file_input, "name", "document")
        file_input.name = target_name
        file_to_send = file_input
        attributes.append(DocumentAttributeFilename(file_name=target_name))
    else:
        logger.error("Unsupported file input type for Developer API: %s", type(file_input))
        return False

    try:
        reply_to = message_thread_id if message_thread_id else None
        await client.send_file(
            entity=chat_id,
            file=file_to_send,
            caption=caption,
            force_document=True,
            reply_to=reply_to,
            attributes=attributes,
            progress_callback=progress_callback,
        )
        logger.info(
            "Uploaded %s via Developer API to chat %s (thread: %s)",
            target_name,
            chat_id,
            message_thread_id,
        )
        return True
    except Exception as e:
        logger.error("Failed to upload %s via Developer API: %s", target_name, e)
        return False
