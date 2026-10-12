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
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from telegram import Bot
from telegram.error import BadRequest, RetryAfter

from ..session import session_manager
from ..transcript_parser import TranscriptParser
from .message_sender import (
    NO_LINK_PREVIEW,
    safe_edit_message,
    safe_send,
    send_document,
    send_photo,
)

logger = logging.getLogger(__name__)

MERGE_CHAR_LIMIT = 3800
STATUS_EDIT_COOLDOWN = 2.5
THOUGHT_EDIT_COOLDOWN = 3.0

_last_status_edit_time: dict[tuple[int, int], float] = {}


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


def _get_status_store_path() -> Path:
    from ..config import config

    return config.config_dir / "status_msg_info.json"


def _load_status_msg_info() -> dict[tuple[int, int], tuple[int, str]]:
    path = _get_status_store_path()
    if not path.exists():
        return {}
    try:
        content = path.read_text(encoding="utf-8")
        if not content.strip():
            return {}
        data = json.loads(content)
        res: dict[tuple[int, int], tuple[int, str]] = {}
        for k, v in data.items():
            u_str, t_str = k.split(":")
            res[(int(u_str), int(t_str))] = (int(v[0]), str(v[1]))
        return res
    except Exception as e:
        logger.debug("Could not load status_msg_info from disk: %s", e)
        return {}


def _save_status_msg_info() -> None:
    path = _get_status_store_path()
    try:
        data = {f"{u}:{t}": list(v) for (u, t), v in _status_msg_info.items()}
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except Exception as e:
        logger.debug("Could not save status_msg_info to disk: %s", e)


# (user_id, thread_id_or_0) -> (message_id, current_text)
_status_msg_info: dict[tuple[int, int], tuple[int, str]] = _load_status_msg_info()


def clear_status_msg_info(user_id: int, thread_id: int | None = None) -> None:
    """Clear status message tracking for a topic."""
    _status_msg_info.pop((user_id, thread_id or 0), None)
    _last_status_edit_time.pop((user_id, thread_id or 0), None)
    _save_status_msg_info()


@dataclass
class ThoughtMessageInfo:
    """Tracking info for the single in-place thought message in a turn."""

    message_id: int
    thoughts: list[str]
    last_edit_time: float = 0.0


# (user_id, thread_id_or_0) -> ThoughtMessageInfo
_thought_msg_info: dict[tuple[int, int], ThoughtMessageInfo] = {}


def clear_thought_msg_info(user_id: int, thread_id: int | None = None) -> None:
    """Clear thought message tracking for a topic."""
    _thought_msg_info.pop((user_id, thread_id or 0), None)


def drain_tasks_for_thread(user_id: int, thread_id: int | None = None) -> None:
    """Drain and discard pending status/content tasks for a specific thread."""
    queue = _user_queues.get(user_id)
    if not queue or queue.empty():
        return
    tid = thread_id or 0
    remaining: list[MessageTask] = []
    while not queue.empty():
        try:
            task = queue.get_nowait()
        except asyncio.QueueEmpty:
            break
        task_tid = task.thread_id or 0
        if task_tid == tid:
            queue.task_done()
        else:
            remaining.append(task)
    for task in remaining:
        queue.put_nowait(task)


async def cleanup_status_for_thread(
    bot: Bot, user_id: int, thread_id: int | None = None
) -> None:
    """Delete any active or orphaned status and thought messages for this thread."""
    clear_active_tool(user_id, thread_id)
    drain_tasks_for_thread(user_id, thread_id)

    chat_id = session_manager.resolve_chat_id(user_id, thread_id)
    tid = thread_id or 0

    if not chat_id:
        clear_thought_msg_info(user_id, thread_id)
        return

    # 1. Delete status message
    matched_keys = [
        k for k in list(_status_msg_info.keys()) if k[1] == tid or k == (user_id, tid)
    ]
    for k in matched_keys:
        info = _status_msg_info.pop(k, None)
        if info:
            msg_id, _ = info
            try:
                await bot.delete_message(chat_id=chat_id, message_id=msg_id)
            except Exception as e:
                logger.debug("Could not delete status message: %s", e)
    if matched_keys:
        _save_status_msg_info()

    # 2. Delete thought message
    thought_keys = [
        k for k in list(_thought_msg_info.keys()) if k[1] == tid or k == (user_id, tid)
    ]
    for k in thought_keys:
        t_info = _thought_msg_info.pop(k, None)
        if t_info:
            try:
                await bot.delete_message(chat_id=chat_id, message_id=t_info.message_id)
            except Exception as e:
                logger.debug("Could not delete thought message: %s", e)


@dataclass
class ActiveToolContext:
    """Active tool action and detail context for a thread."""

    action: str
    detail: str = ""
    timestamp: float = 0.0


_active_tool_contexts: dict[tuple[int, int], ActiveToolContext] = {}


def set_active_tool(
    user_id: int, thread_id: int | None, action: str, detail: str = ""
) -> None:
    """Set the current active tool or state for a thread."""
    key = (user_id, thread_id or 0)
    try:
        loop_time = asyncio.get_event_loop().time()
    except RuntimeError:
        loop_time = 0.0
    _active_tool_contexts[key] = ActiveToolContext(
        action=action, detail=detail, timestamp=loop_time
    )


def get_active_tool(user_id: int, thread_id: int | None) -> ActiveToolContext | None:
    """Get the current active tool or state for a thread."""
    key = (user_id, thread_id or 0)
    return _active_tool_contexts.get(key)


def clear_active_tool(user_id: int, thread_id: int | None) -> None:
    """Clear active tool context for a thread."""
    key = (user_id, thread_id or 0)
    _active_tool_contexts.pop(key, None)


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
                    task, discard_count = _try_merge_status_tasks(queue, task)
                    for _ in range(discard_count):
                        queue.task_done()
                    await _process_status_task(bot, user_id, task)
            except RetryAfter as e:
                from datetime import timedelta

                if isinstance(e.retry_after, timedelta):
                    retry_delay = e.retry_after.total_seconds()
                else:
                    retry_delay = float(e.retry_after)
                logger.warning(
                    "Telegram RateLimit hit: RetryAfter %s seconds", retry_delay
                )
                await asyncio.sleep(min(retry_delay, 30.0) + 0.5)
            except Exception as e:
                logger.error(
                    "Error processing message task for user %d: %s", user_id, e
                )
            finally:
                queue.task_done()
    except asyncio.CancelledError:
        pass


def _try_merge_status_tasks(
    queue: asyncio.Queue[MessageTask], first: MessageTask
) -> tuple[MessageTask, int]:
    """Discard superseded status tasks for the same thread and keep only the latest."""
    latest = first
    discard_count = 0
    remaining: list[MessageTask] = []

    while not queue.empty():
        try:
            next_task = queue.get_nowait()
        except asyncio.QueueEmpty:
            break

        if next_task.task_type == "status" and next_task.thread_id == first.thread_id:
            latest = next_task
            discard_count += 1
        else:
            remaining.append(next_task)
            break

    if remaining:
        rest: list[MessageTask] = []
        while not queue.empty():
            try:
                rest.append(queue.get_nowait())
            except asyncio.QueueEmpty:
                break
        for item in remaining + rest:
            queue.put_nowait(item)

    return latest, discard_count


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
        if (
            nxt.task_type != "content"
            or nxt.thread_id != first.thread_id
            or nxt.content_type != first.content_type
        ):
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


async def _process_thinking_task(
    bot: Bot,
    chat_id: int,
    user_id: int,
    task: MessageTask,
    common_kwargs: dict[str, Any],
) -> None:
    """Send or actively edit the single in-place thought message for this turn."""
    if not task.parts:
        return

    key = (user_id, task.thread_id or 0)
    info = _thought_msg_info.get(key)

    start_tag = TranscriptParser.EXPANDABLE_QUOTE_START
    end_tag = TranscriptParser.EXPANDABLE_QUOTE_END

    new_thoughts: list[str] = []
    for part in task.parts:
        text = part.strip()
        if start_tag in text and end_tag in text:
            inner = text[
                text.index(start_tag) + len(start_tag) : text.index(end_tag)
            ].strip()
        else:
            inner = text.replace("💭 *Thought:*", "").strip()
        if inner and inner not in new_thoughts:
            new_thoughts.append(inner)

    if not new_thoughts:
        return

    if info is None:
        # First thought of the turn: delete preceding status message (e.g. "Working…")
        # so the thought message is positioned cleanly above subsequent tool status lines
        tid = task.thread_id or 0
        matched_keys = [
            k
            for k in list(_status_msg_info.keys())
            if k[1] == tid or k == (user_id, tid)
        ]
        for k in matched_keys:
            status_info = _status_msg_info.pop(k, None)
            if status_info:
                msg_id, _ = status_info
                try:
                    await bot.delete_message(chat_id=chat_id, message_id=msg_id)
                except Exception as e:
                    logger.debug("Could not delete ephemeral status message: %s", e)
        if matched_keys:
            _save_status_msg_info()

        current_thoughts = list(new_thoughts)
    else:
        current_thoughts = list(info.thoughts)
        for nt in new_thoughts:
            if nt not in current_thoughts:
                current_thoughts.append(nt)

    combined_inner = "\n\n".join(current_thoughts)
    max_inner = 3500
    if len(combined_inner) > max_inner:
        combined_inner = (
            "… (earlier thoughts truncated)\n\n" + combined_inner[-(max_inner - 35) :]
        )

    combined_text = (
        f"💭 *Thought:*\n"
        f"{TranscriptParser.EXPANDABLE_QUOTE_START}{combined_inner}{TranscriptParser.EXPANDABLE_QUOTE_END}"
    )

    msg_kwargs = {**common_kwargs, "link_preview_options": NO_LINK_PREVIEW}

    if info is not None:
        now = asyncio.get_event_loop().time()
        if now - info.last_edit_time < THOUGHT_EDIT_COOLDOWN:
            info.thoughts = current_thoughts
            return

        # Actively edit existing thought message in-place
        try:
            res = await safe_edit_message(
                bot,
                chat_id=chat_id,
                message_id=info.message_id,
                text=combined_text,
                **msg_kwargs,
            )
            if res:
                info.thoughts = current_thoughts
                info.last_edit_time = now
            return
        except BadRequest as e:
            err_str = str(e).lower()
            if "message is not modified" in err_str:
                return
            if (
                "message to edit not found" in err_str
                or "message can't be edited" in err_str
            ):
                _thought_msg_info.pop(key, None)
            else:
                logger.debug("Could not edit thought message: %s", e)

    # Send new thought message and track it for this turn
    try:
        sent = await safe_send(bot, chat_id, combined_text, **msg_kwargs)
        if sent:
            _thought_msg_info[key] = ThoughtMessageInfo(
                message_id=sent.message_id,
                thoughts=current_thoughts,
                last_edit_time=asyncio.get_event_loop().time(),
            )
    except Exception as e:
        logger.debug("Could not send thought message: %s", e)


async def _process_content_task(bot: Bot, user_id: int, task: MessageTask) -> None:
    """Send content message parts, photos, and documents, clearing active status lines."""
    chat_id = session_manager.resolve_chat_id(user_id, task.thread_id)
    if not chat_id:
        logger.error(
            "Cannot send message task: chat_id could not be resolved (user_id=%d, thread_id=%s)",
            user_id,
            task.thread_id,
        )
        return

    common_kwargs: dict[str, Any] = {}
    if task.thread_id is not None:
        common_kwargs["message_thread_id"] = task.thread_id

    # If thinking, delegate to single in-place thought message handler
    if task.content_type == "thinking":
        await _process_thinking_task(bot, chat_id, user_id, task, common_kwargs)
        return

    # Ephemeral thought messages: delete when final content is delivered
    tid = task.thread_id or 0
    thought_keys = [
        k for k in list(_thought_msg_info.keys()) if k[1] == tid or k == (user_id, tid)
    ]
    for k in thought_keys:
        t_info = _thought_msg_info.pop(k, None)
        if t_info:
            try:
                await bot.delete_message(chat_id=chat_id, message_id=t_info.message_id)
            except Exception as e:
                logger.debug("Could not delete ephemeral thought message: %s", e)

    # Ephemeral status messages: delete/disappear when final content is delivered
    matched_keys = [
        k for k in list(_status_msg_info.keys()) if k[1] == tid or k == (user_id, tid)
    ]
    for k in matched_keys:
        status_info = _status_msg_info.pop(k, None)
        if status_info:
            msg_id, _ = status_info
            try:
                await bot.delete_message(chat_id=chat_id, message_id=msg_id)
            except Exception as e:
                logger.debug("Could not delete ephemeral status message: %s", e)
    if matched_keys:
        _save_status_msg_info()

    clear_active_tool(user_id, task.thread_id)

    # 1. Send text parts
    if task.parts:
        msg_kwargs = {**common_kwargs, "link_preview_options": NO_LINK_PREVIEW}
        for part in task.parts:
            await safe_send(bot, chat_id, part, **msg_kwargs)

    # 2. Send images if any
    if task.image_data:
        for _, raw_bytes in task.image_data:
            await send_photo(bot, chat_id, raw_bytes, **common_kwargs)

    # 3. Send documents/artifacts if any
    if task.document_data:
        await send_document(bot, chat_id, task.document_data, **common_kwargs)

    # 4. Check and dispatch next queued inbound prompt if any
    from .inbound_queue import dispatch_next_queued_prompt

    await dispatch_next_queued_prompt(bot, user_id, task.thread_id)


async def _process_status_task(bot: Bot, user_id: int, task: MessageTask) -> None:
    """Send or edit status update line in-place."""
    if not task.status_text:
        return

    chat_id = session_manager.resolve_chat_id(user_id, task.thread_id)
    if not chat_id:
        return
    key = (user_id, task.thread_id or 0)
    raw_status = task.status_text.strip()
    if "\n" in raw_status:
        title, detail = raw_status.split("\n", 1)
        title = title.strip()
        detail = detail.strip()
    elif "…" in raw_status:
        parts = raw_status.split("…", 1)
        if len(parts) == 2 and parts[1].strip():
            title = parts[0].strip() + "…"
            detail = parts[1].strip()
        else:
            title = raw_status
            detail = ""
    else:
        title = raw_status
        detail = ""

    from ..terminal_parser import _SPINNERS

    sp = ""
    remainder_title = title
    for spinner_char in _SPINNERS:
        if title.startswith(spinner_char):
            sp = spinner_char
            remainder_title = title[len(spinner_char) :].strip()
            break
    if not sp and any(title.startswith(ic) for ic in ("🧠", "💭", "⚡", "🔍", "⚙️")):
        sp = title[0]
        remainder_title = title[1:].strip()
    if not sp:
        sp = "⚙️"

    clean_title = remainder_title.replace("`", "").replace("*", "").strip()
    if not clean_title:
        clean_title = "Working…"

    prefix = f"{sp} " if sp else ""
    if detail:
        clean_detail = detail.replace("`", "'").strip()
        if len(clean_detail) > 180:
            clean_detail = clean_detail[:177] + "…"
        display_text = f"{prefix}*{clean_title}*\n`{clean_detail}`"
        fallback_text = f"{prefix}{clean_title}\n{clean_detail}"
    else:
        display_text = f"{prefix}*{clean_title}*"
        fallback_text = f"{prefix}{clean_title}"

    status_info = _status_msg_info.get(key)
    if status_info:
        msg_id, last_text = status_info
        if last_text == display_text:
            return

        now = asyncio.get_event_loop().time()
        last_time = _last_status_edit_time.get(key, 0.0)
        if now - last_time < STATUS_EDIT_COOLDOWN:
            return

        try:
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=msg_id,
                text=display_text,
                parse_mode="Markdown",
            )
            _last_status_edit_time[key] = now
            if key in _status_msg_info and _status_msg_info[key][0] == msg_id:
                _status_msg_info[key] = (msg_id, display_text)
                _save_status_msg_info()
            return
        except BadRequest as e:
            err_str = str(e).lower()
            if "message is not modified" in err_str:
                return
            if (
                "message to edit not found" in err_str
                or "message can't be edited" in err_str
            ):
                _status_msg_info.pop(key, None)
                _save_status_msg_info()
            else:
                try:
                    await bot.edit_message_text(
                        chat_id=chat_id,
                        message_id=msg_id,
                        text=fallback_text,
                    )
                    _last_status_edit_time[key] = now
                    if key in _status_msg_info and _status_msg_info[key][0] == msg_id:
                        _status_msg_info[key] = (msg_id, fallback_text)
                        _save_status_msg_info()
                    return
                except Exception:
                    pass

    # Send new status line
    send_kwargs: dict[str, Any] = {"parse_mode": "Markdown"}
    if task.thread_id is not None:
        send_kwargs["message_thread_id"] = task.thread_id

    try:
        msg = await bot.send_message(
            chat_id=chat_id,
            text=display_text,
            **send_kwargs,
        )
        _last_status_edit_time[key] = asyncio.get_event_loop().time()
        _status_msg_info[key] = (msg.message_id, display_text)
        _save_status_msg_info()
    except BadRequest:
        send_kwargs.pop("parse_mode", None)
        try:
            msg = await bot.send_message(
                chat_id=chat_id,
                text=fallback_text,
                **send_kwargs,
            )
            _status_msg_info[key] = (msg.message_id, fallback_text)
            _save_status_msg_info()
        except Exception as ex:
            logger.debug("Could not send fallback status message: %s", ex)
    except Exception as e:
        logger.debug("Could not send status message: %s", e)
