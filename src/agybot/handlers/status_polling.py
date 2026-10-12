"""Terminal status line and interactive prompt polling for agybot.

Continuously monitors active tmux panes:
  - Detects interactive prompts (ask_question, permissions) and displays keyboard
  - Updates running status line (thinking/executing) in Telegram
  - Probes for topic deletion to clean up stale tmux windows
"""

from __future__ import annotations

import asyncio
import logging

from telegram import Bot
from telegram.error import BadRequest

from ..session import session_manager
from ..terminal_parser import extract_interactive_content, parse_status_line
from ..tmux_manager import tmux_manager
from .cleanup import clear_topic_state
from .interactive_ui import (
    clear_interactive_msg,
    get_interactive_window,
    handle_interactive_ui,
)
from .message_queue import enqueue_status_update

logger = logging.getLogger(__name__)

STATUS_POLL_INTERVAL = 1.0
TOPIC_CHECK_INTERVAL = 60.0


async def update_status_message(
    bot: Bot,
    user_id: int,
    window_id: str,
    thread_id: int | None = None,
) -> None:
    """Check terminal pane for interactive UI and status updates."""
    pane_text = await tmux_manager.capture_pane(window_id, start_line=-30)
    if not pane_text:
        return

    # 1. Interactive UI detection
    interactive_content = extract_interactive_content(pane_text)
    if interactive_content:
        await handle_interactive_ui(
            bot,
            user_id,
            window_id,
            interactive_content.content,
            thread_id,
        )
        return
    else:
        # If was in interactive mode but prompt disappeared, clear it
        if get_interactive_window(user_id, thread_id) == window_id:
            await clear_interactive_msg(user_id, bot, thread_id)

    # 2. Status line parsing
    status_line = parse_status_line(pane_text)
    if status_line:
        await enqueue_status_update(
            bot,
            user_id,
            window_id,
            status_line,
            thread_id,
        )


async def status_poll_loop(bot: Bot) -> None:
    """Background polling loop for all bound forum topics."""
    last_topic_check = 0.0

    while True:
        try:
            loop_time = asyncio.get_event_loop().time()
            do_topic_check = (loop_time - last_topic_check) >= TOPIC_CHECK_INTERVAL

            # Iterate over active thread bindings
            for thread_id, window_id in list(session_manager.thread_bindings.items()):
                # Resolve destination chat_id
                chat_id = session_manager.resolve_chat_id(0, thread_id)

                # Periodic topic existence check
                if do_topic_check and chat_id != 0:
                    try:
                        # Attempt harmless call to probe if thread exists
                        await bot.unpin_all_forum_topic_messages(
                            chat_id=chat_id,
                            message_thread_id=thread_id,
                        )
                    except BadRequest as e:
                        if (
                            "thread not found" in str(e).lower()
                            or "topic_deleted" in str(e).lower()
                        ):
                            logger.info(
                                "Topic %d deleted on Telegram, killing window %s",
                                thread_id,
                                window_id,
                            )
                            session_manager.unbind_thread(thread_id)
                            await tmux_manager.kill_window(window_id)
                            await clear_topic_state(0, thread_id, bot)
                            continue
                    except Exception:
                        pass

                # Poll terminal for status / interactive UI
                await update_status_message(bot, 0, window_id, thread_id)

            if do_topic_check:
                last_topic_check = loop_time

        except Exception as e:
            logger.debug("Error in status_poll_loop: %s", e)

        await asyncio.sleep(STATUS_POLL_INTERVAL)
