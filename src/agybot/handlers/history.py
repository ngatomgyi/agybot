"""Message history display with pagination for agybot.

Allows viewing paginated conversation history for the topic's active
Antigravity session.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup

from ..session import session_manager
from ..transcript_parser import TranscriptParser
from .callback_data import CB_HISTORY_NEXT, CB_HISTORY_PREV
from .message_sender import safe_edit, safe_reply, safe_send

logger = logging.getLogger(__name__)

MESSAGES_PER_PAGE = 5


def _build_history_keyboard(
    window_id: str,
    page_index: int,
    total_pages: int,
) -> InlineKeyboardMarkup | None:
    """Build inline keyboard for history pagination."""
    if total_pages <= 1:
        return None

    buttons = []
    if page_index > 0:
        buttons.append(
            InlineKeyboardButton(
                "◀ Older",
                callback_data=f"{CB_HISTORY_PREV}{page_index - 1}:{window_id}"[:64],
            )
        )

    buttons.append(
        InlineKeyboardButton(f"{page_index + 1}/{total_pages}", callback_data="noop")
    )

    if page_index < total_pages - 1:
        buttons.append(
            InlineKeyboardButton(
                "Newer ▶",
                callback_data=f"{CB_HISTORY_NEXT}{page_index + 1}:{window_id}"[:64],
            )
        )

    return InlineKeyboardMarkup([buttons])


async def send_history(
    bot: Bot,
    chat_id: int,
    window_id: str,
    page_index: int = 0,
    message_to_edit: Any = None,
    reply_to_message: Any = None,
    thread_id: int | None = None,
) -> None:
    """Send or edit conversation history page."""
    session = await session_manager.resolve_session_for_window(window_id)
    if not session or not session.file_path:
        text = "❌ No active Antigravity session found for this topic."
        if message_to_edit:
            await safe_edit(message_to_edit, text)
        elif reply_to_message:
            await safe_reply(reply_to_message, text)
        else:
            await safe_send(bot, chat_id, text, message_thread_id=thread_id)
        return

    file_path = Path(session.file_path)
    if not file_path.exists():
        text = "❌ Transcript file not found."
        if message_to_edit:
            await safe_edit(message_to_edit, text)
        elif reply_to_message:
            await safe_reply(reply_to_message, text)
        return

    lines = file_path.read_text(encoding="utf-8", errors="replace").splitlines()
    messages = TranscriptParser.extract_history_messages(lines)

    if not messages:
        text = "ℹ️ No conversation messages recorded yet."
        if message_to_edit:
            await safe_edit(message_to_edit, text)
        elif reply_to_message:
            await safe_reply(reply_to_message, text)
        return

    total_msgs = len(messages)
    total_pages = max(1, (total_msgs + MESSAGES_PER_PAGE - 1) // MESSAGES_PER_PAGE)
    page_index = max(0, min(page_index, total_pages - 1))

    # Pages ordered newest last or newest first?
    start_idx = page_index * MESSAGES_PER_PAGE
    end_idx = min(start_idx + MESSAGES_PER_PAGE, total_msgs)
    page_messages = messages[start_idx:end_idx]

    formatted: list[str] = [
        f"📜 *History for {session.summary}* (Page {page_index + 1}/{total_pages})\n"
    ]

    for m in page_messages:
        prefix = "👤 *User*" if m.message_type == "user" else "🤖 *Antigravity*"
        short_text = m.text[:500] + ("…" if len(m.text) > 500 else "")
        formatted.append(f"{prefix}:\n{short_text}\n")

    full_text = "\n".join(formatted)
    keyboard = _build_history_keyboard(window_id, page_index, total_pages)

    if message_to_edit:
        await safe_edit(message_to_edit, full_text, reply_markup=keyboard)
    elif reply_to_message:
        await safe_reply(reply_to_message, full_text, reply_markup=keyboard)
    else:
        await safe_send(
            bot, chat_id, full_text, reply_markup=keyboard, message_thread_id=thread_id
        )
