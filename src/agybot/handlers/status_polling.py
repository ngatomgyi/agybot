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
from ..terminal_parser import (
    extract_interactive_content,
    extract_spinner_and_status,
)
from ..tmux_manager import tmux_manager
from .cleanup import clear_topic_state
from .interactive_ui import (
    clear_interactive_msg,
    get_interactive_window,
    handle_interactive_ui,
)
from .message_queue import enqueue_status_update, get_active_tool

logger = logging.getLogger(__name__)

STATUS_POLL_INTERVAL = 2.0
TOPIC_CHECK_INTERVAL = 60.0

_last_enqueued_status: dict[tuple[int, int], str] = {}


def clear_last_enqueued_status(user_id: int, thread_id: int | None = None) -> None:
    """Clear cached enqueued status for a thread."""
    _last_enqueued_status.pop((user_id, thread_id or 0), None)


def _get_action_icon(action: str) -> str:
    """Get stable semantic icon without cycling frame-by-frame animations."""
    act = action.lower()
    if any(k in act for k in ("command", "run", "bash", "exec", "git", "pytest", "uv")):
        return "⚙️"
    if any(k in act for k in ("read", "view", "inspect", "search", "grep")):
        return "🔍"
    if any(k in act for k in ("edit", "write", "patch", "modify", "create")):
        return "✏️"
    if any(k in act for k in ("think", "reason", "plan")):
        return "💭"
    return "⏳"


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

    # 2. Live terminal status polling without cycling animation spam
    ctx = get_active_tool(user_id, thread_id)
    if not ctx:
        # Agent is idle (turn finished or interrupted with /esc) - do not poll stale terminal text
        return

    spinner_info = extract_spinner_and_status(pane_text)
    if spinner_info:
        _, term_text = spinner_info
        raw_act = ctx.action if (ctx.action and ctx.action != "Working…") else term_text
        icon = _get_action_icon(raw_act)

        if ctx.detail or ctx.action not in ("Working…", ""):
            # Active tool known from transcript (e.g. running command or reading file)
            if ctx.detail:
                status_text = f"{icon} {ctx.action}\n{ctx.detail}"
            else:
                status_text = f"{icon} {ctx.action}"
        else:
            clean_term = term_text
            if clean_term.endswith("..."):
                clean_term = clean_term[:-3] + "…"
            status_text = f"{icon} {clean_term}"

        # Deduplicate: if semantic status hasn't changed, do NOT re-enqueue
        key = (user_id, thread_id or 0)
        if _last_enqueued_status.get(key) == status_text:
            return

        _last_enqueued_status[key] = status_text
        await enqueue_status_update(
            bot,
            user_id,
            window_id,
            status_text,
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
                user_id = session_manager.resolve_user_id_for_thread(thread_id)
                # Resolve destination chat_id
                chat_id = session_manager.resolve_chat_id(user_id, thread_id)

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
                            await clear_topic_state(user_id, thread_id, bot)
                            continue
                    except Exception:
                        pass

                # Poll terminal for status / interactive UI
                await update_status_message(bot, user_id, window_id, thread_id)

            if do_topic_check:
                last_topic_check = loop_time

        except Exception as e:
            logger.debug("Error in status_poll_loop: %s", e)

        await asyncio.sleep(STATUS_POLL_INTERVAL)
