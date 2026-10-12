"""Inbound prompt queuing for agybot.

Allows users to queue follow-up prompts while Antigravity is busy executing
a turn. Queued prompts are dispatched sequentially as each turn completes,
matching the native Antigravity CLI queuing behavior without interrupting
the active task.
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass
import json
import logging
from pathlib import Path

from telegram import Bot

from ..config import config
from ..session import session_manager
from ..tmux_manager import tmux_manager
from .message_sender import safe_send

logger = logging.getLogger(__name__)


@dataclass
class InboundPrompt:
    """A prompt queued while a session is busy."""

    user_id: int
    thread_id: int | None
    prompt_text: str
    created_at: float


# (user_id, thread_id_or_0) -> list[InboundPrompt]
_inbound_queues: dict[tuple[int, int], list[InboundPrompt]] = {}


def _get_queue_store_path() -> Path:
    return config.config_dir / "inbound_queue.json"


def _load_inbound_queues() -> dict[tuple[int, int], list[InboundPrompt]]:
    path = _get_queue_store_path()
    if not path.exists():
        return {}
    try:
        content = path.read_text(encoding="utf-8")
        if not content.strip():
            return {}
        data = json.loads(content)
        res: dict[tuple[int, int], list[InboundPrompt]] = {}
        for k, items in data.items():
            u_str, t_str = k.split(":")
            key = (int(u_str), int(t_str))
            res[key] = [
                InboundPrompt(
                    user_id=item["user_id"],
                    thread_id=item.get("thread_id"),
                    prompt_text=item["prompt_text"],
                    created_at=item.get("created_at", 0.0),
                )
                for item in items
            ]
        return res
    except Exception as e:
        logger.debug("Could not load inbound_queue from disk: %s", e)
        return {}


def _save_inbound_queues() -> None:
    path = _get_queue_store_path()
    try:
        data = {
            f"{u}:{t}": [asdict(item) for item in items]
            for (u, t), items in _inbound_queues.items()
            if items
        }
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except Exception as e:
        logger.debug("Could not save inbound_queue to disk: %s", e)


_inbound_queues = _load_inbound_queues()


def queue_inbound_prompt(
    user_id: int,
    thread_id: int | None,
    prompt_text: str,
) -> int:
    """Queue a prompt for execution once the current turn completes.

    Returns the 1-based queue position.
    """
    key = (user_id, thread_id or 0)
    try:
        loop_time = asyncio.get_event_loop().time()
    except RuntimeError:
        loop_time = 0.0

    prompt = InboundPrompt(
        user_id=user_id,
        thread_id=thread_id,
        prompt_text=prompt_text,
        created_at=loop_time,
    )
    queue = _inbound_queues.setdefault(key, [])
    queue.append(prompt)
    _save_inbound_queues()
    return len(queue)


def get_queued_prompts(user_id: int, thread_id: int | None) -> list[InboundPrompt]:
    """Get all queued prompts for a thread."""
    key = (user_id, thread_id or 0)
    return list(_inbound_queues.get(key, []))


def has_queued_inbound_prompts(user_id: int, thread_id: int | None) -> bool:
    """Check if thread has any queued inbound prompts."""
    key = (user_id, thread_id or 0)
    return bool(_inbound_queues.get(key))


def pop_next_inbound_prompt(
    user_id: int, thread_id: int | None
) -> InboundPrompt | None:
    """Pop and return the next queued inbound prompt for a thread."""
    key = (user_id, thread_id or 0)
    queue = _inbound_queues.get(key)
    if not queue:
        return None
    prompt = queue.pop(0)
    if not queue:
        _inbound_queues.pop(key, None)
    _save_inbound_queues()
    return prompt


def clear_inbound_queue(user_id: int, thread_id: int | None) -> int:
    """Clear all queued inbound prompts for a thread. Returns number of cleared prompts."""
    key = (user_id, thread_id or 0)
    removed = len(_inbound_queues.pop(key, []))
    if removed:
        _save_inbound_queues()
    return removed


def is_thread_busy(user_id: int, thread_id: int | None) -> bool:
    """Check if a thread is actively running a task or has queued prompts waiting."""
    from .message_queue import get_active_tool

    ctx = get_active_tool(user_id, thread_id)
    if ctx is not None:
        return True
    return has_queued_inbound_prompts(user_id, thread_id)


async def dispatch_next_queued_prompt(
    bot: Bot, user_id: int, thread_id: int | None
) -> bool:
    """Dispatch the next queued prompt for a thread if available.

    Sends keys to tmux and initializes the active tool status.
    """
    next_prompt = pop_next_inbound_prompt(user_id, thread_id)
    if not next_prompt:
        return False

    wid = session_manager.get_window_for_thread(thread_id)
    if not wid:
        return False

    from .message_queue import enqueue_status_update, set_active_tool

    chat_id = session_manager.resolve_chat_id(user_id, thread_id)
    clean_prompt = next_prompt.prompt_text.replace("\n", " ")

    # 1. Inform topic that queued prompt is beginning execution
    if chat_id:
        preview = next_prompt.prompt_text
        if len(preview) > 120:
            preview = preview[:117] + "…"
        kwargs = {}
        if thread_id is not None:
            kwargs["message_thread_id"] = thread_id
        try:
            await safe_send(
                bot,
                chat_id,
                f"▶️ *Running queued prompt:*\n`{preview}`",
                **kwargs,
            )
        except Exception as e:
            logger.debug("Could not send queued prompt dispatch notice: %s", e)

    # 2. Brief settling pause then send keys to tmux
    await asyncio.sleep(0.3)
    await tmux_manager.send_keys(wid, clean_prompt, enter=True)

    # 3. Set active tool context and enqueue initial loading status
    set_active_tool(user_id, thread_id, "Working…", "")
    await enqueue_status_update(
        bot=bot,
        user_id=user_id,
        window_id=wid,
        status_text="⠋ Working…",
        thread_id=thread_id,
    )
    return True
