"""Topic state cleanup handler for agybot.

Cleans up cached messages, interactive UI state, and status tracking
when a Telegram topic is closed, unbound, or deleted.
"""

from __future__ import annotations

from typing import Any

from telegram import Bot

from .interactive_ui import clear_interactive_msg
from .message_queue import clear_status_msg_info


async def clear_topic_state(
    user_id: int,
    thread_id: int,
    bot: Bot | None = None,
    user_data: dict[str, Any] | None = None,
) -> None:
    """Clear all memory and UI state associated with a topic."""
    clear_status_msg_info(user_id, thread_id)
    await clear_interactive_msg(user_id, bot, thread_id)

    if user_data is not None:
        if user_data.get("_pending_thread_id") == thread_id:
            user_data.pop("_pending_thread_id", None)
            user_data.pop("_pending_thread_text", None)
