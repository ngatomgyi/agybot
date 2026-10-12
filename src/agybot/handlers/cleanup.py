"""Topic state cleanup handler for agybot.

Cleans up cached messages, interactive UI state, and status tracking
when a Telegram topic is closed, unbound, or deleted.
"""

from __future__ import annotations

from typing import Any

from telegram import Bot

from .inbound_queue import clear_inbound_queue
from .interactive_ui import clear_interactive_msg
from .message_queue import (
    clear_active_tool,
    clear_status_msg_info,
    clear_thought_msg_info,
    drain_tasks_for_thread,
)


async def clear_topic_state(
    user_id: int,
    thread_id: int,
    bot: Bot | None = None,
    user_data: dict[str, Any] | None = None,
) -> None:
    """Clear all memory and UI state associated with a topic."""
    from .status_polling import clear_last_enqueued_status

    clear_status_msg_info(user_id, thread_id)
    clear_thought_msg_info(user_id, thread_id)
    clear_last_enqueued_status(user_id, thread_id)
    clear_active_tool(user_id, thread_id)
    drain_tasks_for_thread(user_id, thread_id)
    clear_inbound_queue(user_id, thread_id)
    await clear_interactive_msg(user_id, bot, thread_id)

    if user_data is not None:
        if user_data.get("_pending_thread_id") == thread_id:
            user_data.pop("_pending_thread_id", None)
            user_data.pop("_pending_thread_text", None)
