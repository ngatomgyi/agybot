"""Interactive UI handling for Antigravity terminal prompts.

Displays interactive terminal prompts with an inline keyboard:
  - Navigation keys: Up, Down, Left, Right
  - Action keys: Enter, Space, Tab, Esc
  - In-place terminal updates as selections change
"""

from __future__ import annotations

import logging
from typing import Any

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest
from telegram.ext import ContextTypes

from ..session import session_manager
from ..terminal_parser import extract_interactive_content
from ..tmux_manager import tmux_manager
from .callback_data import (
    CB_ASK_DOWN,
    CB_ASK_ENTER,
    CB_ASK_ESC,
    CB_ASK_LEFT,
    CB_ASK_REFRESH,
    CB_ASK_RIGHT,
    CB_ASK_SPACE,
    CB_ASK_TAB,
    CB_ASK_UP,
)
from .message_sender import NO_LINK_PREVIEW

logger = logging.getLogger(__name__)

# (user_id, thread_id_or_0) -> message_id
_interactive_msgs: dict[tuple[int, int], int] = {}
# (user_id, thread_id_or_0) -> window_id
_interactive_mode: dict[tuple[int, int], str] = {}


def get_interactive_window(user_id: int, thread_id: int | None = None) -> str | None:
    """Get the window_id for user's interactive mode."""
    return _interactive_mode.get((user_id, thread_id or 0))


def set_interactive_mode(
    user_id: int, window_id: str, thread_id: int | None = None
) -> None:
    """Set interactive mode for a user and topic."""
    _interactive_mode[(user_id, thread_id or 0)] = window_id


def clear_interactive_mode(user_id: int, thread_id: int | None = None) -> None:
    """Clear interactive mode without deleting the message."""
    _interactive_mode.pop((user_id, thread_id or 0), None)


def build_interactive_keyboard(window_id: str) -> InlineKeyboardMarkup:
    """Build inline keyboard for interactive terminal navigation."""
    row1 = [
        InlineKeyboardButton("⬆️", callback_data=f"{CB_ASK_UP}{window_id}"),
        InlineKeyboardButton("⬇️", callback_data=f"{CB_ASK_DOWN}{window_id}"),
        InlineKeyboardButton("⬅️", callback_data=f"{CB_ASK_LEFT}{window_id}"),
        InlineKeyboardButton("➡️", callback_data=f"{CB_ASK_RIGHT}{window_id}"),
    ]
    row2 = [
        InlineKeyboardButton(
            "Select (Space)", callback_data=f"{CB_ASK_SPACE}{window_id}"
        ),
        InlineKeyboardButton("Tab", callback_data=f"{CB_ASK_TAB}{window_id}"),
        InlineKeyboardButton(
            "Confirm (Enter)", callback_data=f"{CB_ASK_ENTER}{window_id}"
        ),
    ]
    row3 = [
        InlineKeyboardButton(
            "❌ Cancel (Esc)", callback_data=f"{CB_ASK_ESC}{window_id}"
        ),
        InlineKeyboardButton(
            "🔄 Refresh", callback_data=f"{CB_ASK_REFRESH}{window_id}"
        ),
    ]
    return InlineKeyboardMarkup([row1, row2, row3])


async def handle_interactive_ui(
    bot: Bot,
    user_id: int,
    window_id: str,
    content: str,
    thread_id: int | None = None,
) -> None:
    """Send or update the interactive prompt message in Telegram."""
    key = (user_id, thread_id or 0)
    chat_id = session_manager.resolve_chat_id(user_id, thread_id)
    text = f"```\n{content}\n```\n_Use the buttons below or type an option in chat:_"
    keyboard = build_interactive_keyboard(window_id)

    msg_id = _interactive_msgs.get(key)
    if msg_id:
        try:
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=msg_id,
                text=text,
                parse_mode="Markdown",
                reply_markup=keyboard,
                link_preview_options=NO_LINK_PREVIEW,
            )
            set_interactive_mode(user_id, window_id, thread_id)
            return
        except BadRequest as e:
            if "message is not modified" in str(e).lower():
                return
            logger.debug("Failed to edit interactive message, sending new: %s", e)

    # Send new message
    send_kwargs: dict[str, Any] = {
        "reply_markup": keyboard,
        "parse_mode": "Markdown",
        "link_preview_options": NO_LINK_PREVIEW,
    }
    if thread_id is not None:
        send_kwargs["message_thread_id"] = thread_id

    try:
        msg = await bot.send_message(
            chat_id=chat_id,
            text=text,
            **send_kwargs,
        )
        _interactive_msgs[key] = msg.message_id
        set_interactive_mode(user_id, window_id, thread_id)
    except Exception as e:
        logger.error("Failed to send interactive message: %s", e)


async def clear_interactive_msg(
    user_id: int, bot: Bot | None = None, thread_id: int | None = None
) -> None:
    """Delete interactive UI message and clear state."""
    key = (user_id, thread_id or 0)
    clear_interactive_mode(user_id, thread_id)
    msg_id = _interactive_msgs.pop(key, None)

    if msg_id and bot:
        chat_id = session_manager.resolve_chat_id(user_id, thread_id)
        try:
            await bot.delete_message(chat_id=chat_id, message_id=msg_id)
        except Exception:
            pass


async def handle_interactive_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    action: str,
    window_id: str,
) -> None:
    """Handle interactive navigation button press."""
    query = update.callback_query
    if not query:
        return

    await query.answer()
    thread_id = (
        update.effective_message.message_thread_id if update.effective_message else None
    )
    user_id = query.from_user.id

    key_map = {
        "up": ("Up", False, False),
        "down": ("Down", False, False),
        "left": ("Left", False, False),
        "right": ("Right", False, False),
        "enter": ("Enter", False, False),
        "esc": ("Escape", False, False),
        "spc": ("Space", False, False),
        "tab": ("Tab", False, False),
    }

    if action in key_map:
        key, enter, literal = key_map[action]
        await tmux_manager.send_keys(window_id, key, enter=enter, literal=literal)

    # Capture pane and update
    pane_text = await tmux_manager.capture_pane(window_id, start_line=-30)
    interactive_content = extract_interactive_content(pane_text)

    if interactive_content:
        await handle_interactive_ui(
            context.bot,
            user_id,
            window_id,
            interactive_content.content,
            thread_id,
        )
    else:
        # Prompt exited
        await clear_interactive_msg(user_id, context.bot, thread_id)
