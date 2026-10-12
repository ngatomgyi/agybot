"""Per-user message queue with merging and rate-limiting for agybot.

Provides:
  - Per-user FIFO queue with automatic consecutive message merging (up to 3800 chars)
  - Dedicated worker task per user
  - Retry-After backoff handling
  - Inline status message tracking and editing
  - Outbound image and artifact document dispatch
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

from telegram import Bot
from telegram.error import BadRequest, RetryAfter

from ..session import session_manager
from .message_sender import (
    NO_LINK_PREVIEW,
    PARSE_MODE,
    send_document,
    send_photo,
    send_with_fallback,
)

logger = logging.getLogger(__name__)

MERGE_CHAR_LIMIT = 3800


@dataclass
class MessageTask:
    """A task to be dispatched to Telegram."""

    task_type: str  # "content" or "status"
    window_id: str = ""
    parts: list[str] | None = None
    content_type: str = "text"
    thread_id: int | None = None
    image_data: list[tuple[str, bytes]] | None = None
    document_data: list[tuple[str, bytes]] | None = None
    status_text: str | None = None


# user_id -> Queue[MessageTask]
_user_queues: dict[int, asyncio.Queue[MessageTask]] = {}
# user_id -> Task
_worker_tasks: dict[int, asyncio.Task[None]] = {}
# (user_id, thread_id_or_0) -> (message_id, current_text)
_status_msg_info: dict[tuple[int, int], tuple[int, str]] = {}


def clear_status_msg_info(user_id: int, thread_id: int | None = None) -> None:
    """Clear status message tracking for a topic."""
    _status_msg_info.pop((user_id, thread_id or 0), None)


def get_message_queue(user_id: int, bot: Bot) -> asyncio.Queue[MessageTask]:
    """Get or create the message queue and worker for a user."""
    if user_id not in _user_queues:
        _user_queues[user_id] = asyncio.Queue()
        _worker_tasks[user_id] = asyncio.create_task(_worker_loop(user_id, bot))
    return _user_queues[user_id]


async def enqueue_content_message(
    bot: Bot,
    user_id: int,
    window_id: str,
    parts: list[str],
    content_type: str = "text",
    thread_id: int | None = None,
    image_data: list[tuple[str, bytes]] | None = None,
    document_data: list[tuple[str, bytes]] | None = None,
) -> None:
    """Enqueue a content message for delivery."""
    queue = get_message_queue(user_id, bot)
    task = MessageTask(
        task_type="content",
        window_id=window_id,
        parts=parts,
        content_type=content_type,
        thread_id=thread_id,
        image_data=image_data,
        document_data=document_data,
    )
    queue.put_nowait(task)


async def enqueue_status_update(
    bot: Bot,
    user_id: int,
    window_id: str,
    status_text: str,
    thread_id: int | None = None,
) -> None:
    """Enqueue a status message update."""
    queue = get_message_queue(user_id, bot)
    task = MessageTask(
        task_type="status",
        window_id=window_id,
        thread_id=thread_id,
        status_text=status_text,
    )
    queue.put_nowait(task)


async def _worker_loop(user_id: int, bot: Bot) -> None:
    """Worker loop processing messages for a single user."""
    queue = _user_queues[user_id]
    try:
        while True:
            task = await queue.get()
            try:
                if task.task_type == "content":
                    task, merge_count = _try_merge_content_tasks(queue, task)
                    for _ in range(merge_count):
                        queue.task_done()
                    await _process_content_task(bot, user_id, task)
                elif task.task_type == "status":
                    await _process_status_task(bot, user_id, task)
            except RetryAfter as e:
                from datetime import timedelta

                if isinstance(e.retry_after, timedelta):
                    retry_delay = e.retry_after.total_seconds()
                else:
                    retry_delay = float(e.retry_after)
                logger.warning("Telegram RateLimit: sleeping %s seconds", retry_delay)
                await asyncio.sleep(retry_delay + 0.5)
            except Exception as e:
                logger.error(
                    "Error processing message task for user %d: %s", user_id, e
                )
            finally:
                queue.task_done()
    except asyncio.CancelledError:
        pass


def _try_merge_content_tasks(
    queue: asyncio.Queue[MessageTask], first: MessageTask
) -> tuple[MessageTask, int]:
    """Merge consecutive text content tasks destined for the same thread."""
    if not first.parts:
        return first, 0

    merged_parts = list(first.parts)
    merged_images = list(first.image_data) if first.image_data else []
    merged_docs = list(first.document_data) if first.document_data else []
    curr_len = sum(len(p) for p in merged_parts)
    merge_count = 0

    items: list[MessageTask] = []
    while not queue.empty():
        try:
            items.append(queue.get_nowait())
        except asyncio.QueueEmpty:
            break

    remaining: list[MessageTask] = []
    for i, nxt in enumerate(items):
        if nxt.task_type != "content" or nxt.thread_id != first.thread_id:
            remaining = items[i:]
            break
        nxt_len = sum(len(p) for p in nxt.parts) if nxt.parts else 0
        if curr_len + nxt_len > MERGE_CHAR_LIMIT:
            remaining = items[i:]
            break

        if nxt.parts:
            merged_parts.extend(nxt.parts)
            curr_len += nxt_len
        if nxt.image_data:
            merged_images.extend(nxt.image_data)
        if nxt.document_data:
            merged_docs.extend(nxt.document_data)
        merge_count += 1

    # Put unmerged items back into the queue
    for item in remaining:
        queue.put_nowait(item)

    return (
        MessageTask(
            task_type="content",
            window_id=first.window_id,
            parts=merged_parts,
            content_type=first.content_type,
            thread_id=first.thread_id,
            image_data=merged_images or None,
            document_data=merged_docs or None,
        ),
        merge_count,
    )


async def _process_content_task(bot: Bot, user_id: int, task: MessageTask) -> None:
    """Send content message parts, photos, and documents."""
    chat_id = session_manager.resolve_chat_id(user_id, task.thread_id)
    send_kwargs: dict[str, Any] = {
        "link_preview_options": NO_LINK_PREVIEW,
    }
    if task.thread_id is not None:
        send_kwargs["message_thread_id"] = task.thread_id

    # 1. Send text parts
    if task.parts:
        for part in task.parts:

            async def _send(t: str) -> None:
                await bot.send_message(
                    chat_id=chat_id,
                    text=t,
                    parse_mode=PARSE_MODE,
                    **send_kwargs,
                )

            await send_with_fallback(_send, part)

    # 2. Send images if any
    if task.image_data:
        for _, raw_bytes in task.image_data:
            await send_photo(bot, chat_id, raw_bytes, **send_kwargs)

    # 3. Send documents/artifacts if any
    if task.document_data:
        await send_document(bot, chat_id, task.document_data, **send_kwargs)


async def _process_status_task(bot: Bot, user_id: int, task: MessageTask) -> None:
    """Send or edit status update line."""
    if not task.status_text:
        return

    chat_id = session_manager.resolve_chat_id(user_id, task.thread_id)
    key = (user_id, task.thread_id or 0)
    text = f"`{task.status_text}`"

    status_info = _status_msg_info.get(key)
    if status_info:
        msg_id, last_text = status_info
        if last_text == text:
            return
        try:
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=msg_id,
                text=text,
                parse_mode="Markdown",
            )
            _status_msg_info[key] = (msg_id, text)
            return
        except BadRequest as e:
            if "message is not modified" in str(e).lower():
                return
            logger.debug("Could not edit status message: %s", e)

    # Send new status line
    send_kwargs: dict[str, Any] = {"parse_mode": "Markdown"}
    if task.thread_id is not None:
        send_kwargs["message_thread_id"] = task.thread_id

    try:
        msg = await bot.send_message(
            chat_id=chat_id,
            text=text,
            **send_kwargs,
        )
        _status_msg_info[key] = (msg.message_id, text)
    except Exception as e:
        logger.debug("Could not send status message: %s", e)
