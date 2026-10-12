"""Main Telegram Bot setup, command handlers, and dispatchers for agybot.

Bridges Telegram Forum topics to Antigravity CLI sessions via tmux windows:
  - 1 Forum Topic = 1 Tmux Window = 1 Antigravity Session
  - Inbound messages forwarded via tmux_manager.send_keys()
  - Outbound agent responses streamed from transcript.jsonl
  - Full screenshot capture, directory browsing, and session resume capabilities
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from telegram import (
    Bot,
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.ext import (
    AIORateLimiter,
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from .config import config
from .handlers.callback_data import (
    CB_DIR_CANCEL,
    CB_DIR_CONFIRM,
    CB_DIR_PAGE,
    CB_DIR_SELECT,
    CB_DIR_UP,
    CB_HISTORY_NEXT,
    CB_HISTORY_PREV,
    CB_KEYS_PREFIX,
    CB_SCREENSHOT_REFRESH,
    CB_SESSION_CANCEL,
    CB_SESSION_NEW,
    CB_SESSION_SELECT,
    CB_WIN_BIND,
    CB_WIN_CANCEL,
    CB_WIN_NEW,
)
from .handlers.cleanup import clear_topic_state
from .handlers.directory_browser import (
    BROWSE_DIRS_KEY,
    BROWSE_PAGE_KEY,
    BROWSE_PATH_KEY,
    SESSIONS_KEY,
    STATE_BROWSING_DIRECTORY,
    STATE_KEY,
    STATE_SELECTING_SESSION,
    STATE_SELECTING_WINDOW,
    UNBOUND_WINDOWS_KEY,
    build_directory_browser,
    build_session_picker,
    build_window_picker,
    clear_browse_state,
)
from .handlers.history import send_history
from .handlers.interactive_ui import handle_interactive_callback
from .handlers.message_queue import enqueue_content_message
from .handlers.message_sender import (
    safe_edit,
    safe_reply,
    send_document,
    send_photo,
)
from .handlers.response_builder import build_response_parts
from .handlers.status_polling import status_poll_loop
from .screenshot import text_to_image_async
from .session import session_manager
from .session_monitor import NewMessage, session_monitor
from .tmux_manager import tmux_manager

logger = logging.getLogger(__name__)


def is_user_allowed(user_id: int) -> bool:
    """Check if Telegram user is allowed."""
    return user_id in config.allowed_users


def _get_thread_id(update: Update) -> int | None:
    """Extract message_thread_id from update."""
    if update.effective_message:
        return update.effective_message.message_thread_id
    return None


# --- Command Handlers ---


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /start command."""
    user = update.effective_user
    if not user or not is_user_allowed(user.id):
        return
    if not update.message:
        return

    text = (
        "🚀 *Welcome to Antigravity Bot (agybot)*\n\n"
        "This bot bridges Telegram Forum topics to autonomous Antigravity CLI sessions in tmux.\n\n"
        "*Core Features:*\n"
        "• *1 Topic = 1 Tmux Window = 1 Session*\n"
        "• Runs in *accept-edits mode* with autonomous permissions\n"
        "• Live response streaming, code artifacts, and terminal controls\n\n"
        "*Commands:*\n"
        "• `/screenshot` - View current terminal screen\n"
        "• `/history` - View conversation history in topic\n"
        "• `/artifacts` - Download all generated code/markdown artifacts\n"
        "• `/esc` - Interrupt current execution turn\n"
        "• `/unbind` - Unbind topic from window\n"
        "• `/kill` - Kill tmux window and unbind topic\n"
        "• `/clear` - Clear conversation context\n"
        "• `/usage` - View session stats\n\n"
        "_To get started, create a new Forum Topic in this supergroup and send a message!_"
    )
    await safe_reply(update.message, text)


async def esc_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /esc command to interrupt agent turn."""
    user = update.effective_user
    if not user or not is_user_allowed(user.id) or not update.message:
        return

    thread_id = _get_thread_id(update)
    wid = session_manager.get_window_for_thread(thread_id)
    if not wid:
        await safe_reply(update.message, "❌ No window bound to this topic.")
        return

    success = await tmux_manager.send_interrupt(wid)
    if success:
        await safe_reply(update.message, "⏹ Sent Escape interrupt to Antigravity.")
    else:
        await safe_reply(update.message, "❌ Failed to send interrupt.")


async def unbind_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /unbind command."""
    user = update.effective_user
    if not user or not is_user_allowed(user.id) or not update.message:
        return

    thread_id = _get_thread_id(update)
    if thread_id is None:
        await safe_reply(update.message, "❌ /unbind can only be run in a forum topic.")
        return

    wid = session_manager.unbind_thread(thread_id)
    if wid:
        await clear_topic_state(user.id, thread_id, context.bot, context.user_data)
        await safe_reply(
            update.message,
            f"🔓 Unbound topic from window `{wid}`. Window is still running in tmux.",
        )
    else:
        await safe_reply(update.message, "ℹ️ No window was bound to this topic.")


async def kill_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /kill command."""
    user = update.effective_user
    if not user or not is_user_allowed(user.id) or not update.message:
        return

    thread_id = _get_thread_id(update)
    if thread_id is None:
        await safe_reply(update.message, "❌ /kill can only be run in a forum topic.")
        return

    wid = session_manager.unbind_thread(thread_id)
    if wid:
        await tmux_manager.kill_window(wid)
        await clear_topic_state(user.id, thread_id, context.bot, context.user_data)
        await safe_reply(
            update.message, f"💀 Terminated window `{wid}` and unbound topic."
        )
    else:
        await safe_reply(update.message, "ℹ️ No window was bound to this topic.")


async def clear_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /clear command."""
    user = update.effective_user
    if not user or not is_user_allowed(user.id) or not update.message:
        return

    thread_id = _get_thread_id(update)
    wid = session_manager.get_window_for_thread(thread_id)
    if not wid:
        await safe_reply(update.message, "❌ No window bound to this topic.")
        return

    await tmux_manager.send_keys(wid, "/clear", enter=True)
    await safe_reply(update.message, "🧹 Sent /clear to Antigravity session.")


async def usage_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /usage command."""
    user = update.effective_user
    if not user or not is_user_allowed(user.id) or not update.message:
        return

    thread_id = _get_thread_id(update)
    wid = session_manager.get_window_for_thread(thread_id)
    if not wid:
        await safe_reply(update.message, "❌ No window bound to this topic.")
        return

    session = await session_manager.resolve_session_for_window(wid)
    if session:
        text = (
            f"📊 *Session Info*\n"
            f"• ID: `{session.session_id}`\n"
            f"• Title: *{session.summary}*\n"
            f"• Steps/Messages: `{session.message_count}`\n"
            f"• Workspace: `{session.cwd}`"
        )
        await safe_reply(update.message, text)
    else:
        await safe_reply(update.message, "ℹ️ No session active yet.")


async def artifacts_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /artifacts command to send all markdown & code artifacts."""
    user = update.effective_user
    if not user or not is_user_allowed(user.id) or not update.message:
        return

    thread_id = _get_thread_id(update)
    wid = session_manager.get_window_for_thread(thread_id)
    if not wid:
        await safe_reply(update.message, "❌ No window bound to this topic.")
        return

    session = await session_manager.resolve_session_for_window(wid)
    if not session or not session.file_path:
        await safe_reply(update.message, "❌ No active session found.")
        return

    p = Path(session.file_path)
    brain_dir = p.parents[2] if "/brain/" in str(p) else None
    artifacts: list[tuple[str, bytes]] = []

    if brain_dir and brain_dir.is_dir():
        for md_file in brain_dir.glob("*.md"):
            if not md_file.name.endswith(".metadata.json") and md_file.is_file():
                try:
                    artifacts.append((md_file.name, md_file.read_bytes()))
                except Exception as e:
                    logger.warning("Error reading artifact %s: %s", md_file, e)

    if not artifacts:
        await safe_reply(update.message, "ℹ️ No artifacts found in this session yet.")
        return

    chat_id = session_manager.resolve_chat_id(user.id, thread_id)
    await safe_reply(update.message, f"📦 Sending {len(artifacts)} artifact(s)...")
    await send_document(
        bot=context.bot,
        chat_id=chat_id,
        document_data=artifacts,
        message_thread_id=thread_id,
    )


def _build_screenshot_keyboard(window_id: str) -> InlineKeyboardMarkup:
    """Build control keyboard for /screenshot."""
    row1 = [
        InlineKeyboardButton("⬆️", callback_data=f"{CB_KEYS_PREFIX}up:{window_id}"),
        InlineKeyboardButton("⬇️", callback_data=f"{CB_KEYS_PREFIX}down:{window_id}"),
        InlineKeyboardButton("⬅️", callback_data=f"{CB_KEYS_PREFIX}left:{window_id}"),
        InlineKeyboardButton("➡️", callback_data=f"{CB_KEYS_PREFIX}right:{window_id}"),
    ]
    row2 = [
        InlineKeyboardButton(
            "Enter", callback_data=f"{CB_KEYS_PREFIX}enter:{window_id}"
        ),
        InlineKeyboardButton("Esc", callback_data=f"{CB_KEYS_PREFIX}esc:{window_id}"),
        InlineKeyboardButton(
            "Space", callback_data=f"{CB_KEYS_PREFIX}space:{window_id}"
        ),
        InlineKeyboardButton("Tab", callback_data=f"{CB_KEYS_PREFIX}tab:{window_id}"),
    ]
    row3 = [
        InlineKeyboardButton("y", callback_data=f"{CB_KEYS_PREFIX}y:{window_id}"),
        InlineKeyboardButton("n", callback_data=f"{CB_KEYS_PREFIX}n:{window_id}"),
        InlineKeyboardButton(
            "🔄 Refresh", callback_data=f"{CB_SCREENSHOT_REFRESH}{window_id}"
        ),
    ]
    return InlineKeyboardMarkup([row1, row2, row3])


async def screenshot_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle /screenshot command."""
    user = update.effective_user
    if not user or not is_user_allowed(user.id) or not update.message:
        return

    thread_id = _get_thread_id(update)
    wid = session_manager.get_window_for_thread(thread_id)
    if not wid:
        await safe_reply(update.message, "❌ No window bound to this topic.")
        return

    pane_text = await tmux_manager.capture_pane(wid, start_line=-40, with_ansi=True)
    img_bytes = await text_to_image_async(pane_text, font_size=15, with_ansi=True)
    chat_id = session_manager.resolve_chat_id(user.id, thread_id)
    keyboard = _build_screenshot_keyboard(wid)

    await send_photo(
        bot=context.bot,
        chat_id=chat_id,
        image_bytes=img_bytes,
        reply_markup=keyboard,
        message_thread_id=thread_id,
    )


async def history_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /history command."""
    user = update.effective_user
    if not user or not is_user_allowed(user.id) or not update.message:
        return

    thread_id = _get_thread_id(update)
    wid = session_manager.get_window_for_thread(thread_id)
    if not wid:
        await safe_reply(update.message, "❌ No window bound to this topic.")
        return

    chat_id = session_manager.resolve_chat_id(user.id, thread_id)
    await send_history(
        bot=context.bot,
        chat_id=chat_id,
        window_id=wid,
        page_index=0,
        reply_to_message=update.message,
        thread_id=thread_id,
    )


# --- Inbound Text Messages ---


async def handle_text_message(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle regular text messages sent by user."""
    user = update.effective_user
    if not user or not is_user_allowed(user.id) or not update.message:
        return

    thread_id = _get_thread_id(update)
    chat_id = update.message.chat_id

    # Store group chat_id for routing
    if thread_id is not None:
        session_manager.set_group_chat_id(user.id, thread_id, chat_id)

    text = update.message.text
    if not text:
        return

    wid = session_manager.get_window_for_thread(thread_id)

    # 1. Topic already bound to a window -> forward keys
    if wid:
        await tmux_manager.send_keys(wid, text, enter=True)
        return

    # 2. Topic is unbound -> Show Window Picker or Directory Browser
    if thread_id is None:
        await safe_reply(
            update.message,
            "ℹ️ agybot operates inside Telegram Forum topics.\n"
            "Please create a topic and send your message there!",
        )
        return

    # Cache pending message text
    user_data = context.user_data if context.user_data is not None else {}
    user_data["_pending_thread_id"] = thread_id
    user_data["_pending_thread_text"] = text

    # Check for unbound windows in agybot session
    all_windows = await tmux_manager.list_windows()
    bound_wids = set(session_manager.thread_bindings.values())
    unbound: list[tuple[str, str, str]] = [
        (w.window_id, w.window_name, w.cwd)
        for w in all_windows
        if w.window_id not in bound_wids
        and w.window_name != config.tmux_main_window_name
    ]

    if unbound:
        picker_text, markup = build_window_picker(unbound)
        user_data[STATE_KEY] = STATE_SELECTING_WINDOW
        user_data[UNBOUND_WINDOWS_KEY] = unbound
        await safe_reply(update.message, picker_text, reply_markup=markup)
    else:
        # No unbound windows: open directory browser from home
        current_path = Path.home()
        browser_text, markup, subdirs = build_directory_browser(current_path, page=0)
        user_data[STATE_KEY] = STATE_BROWSING_DIRECTORY
        user_data[BROWSE_PATH_KEY] = str(current_path)
        user_data[BROWSE_PAGE_KEY] = 0
        user_data[BROWSE_DIRS_KEY] = subdirs
        await safe_reply(update.message, browser_text, reply_markup=markup)


# --- Callback Queries ---


async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Route callback queries from inline buttons."""
    query = update.callback_query
    if not query or not query.data:
        return

    user = query.from_user
    if not is_user_allowed(user.id):
        await query.answer("Access denied", show_alert=True)
        return

    data = query.data
    thread_id = (
        update.effective_message.message_thread_id if update.effective_message else None
    )
    user_data = context.user_data if context.user_data is not None else {}

    # 1. No-op
    if data == "noop":
        await query.answer()
        return

    # 2. Window Picker callbacks
    if data.startswith(CB_WIN_BIND):
        idx = int(data[len(CB_WIN_BIND) :])
        unbound = user_data.get(UNBOUND_WINDOWS_KEY, [])
        if 0 <= idx < len(unbound) and thread_id is not None:
            wid, name, cwd = unbound[idx]
            session_manager.bind_thread(thread_id, wid)
            await query.answer(f"Attached window {name}")
            await safe_edit(
                query.message, f"🔗 Bound topic to tmux window `{name}` ({wid})"
            )
            # Forward pending text if any
            pending_text = user_data.pop("_pending_thread_text", None)
            if pending_text:
                await tmux_manager.send_keys(wid, pending_text, enter=True)
            clear_browse_state(user_data)
        return

    if data == CB_WIN_NEW:
        await query.answer()
        current_path = Path.home()
        browser_text, markup, subdirs = build_directory_browser(current_path, page=0)
        user_data[STATE_KEY] = STATE_BROWSING_DIRECTORY
        user_data[BROWSE_PATH_KEY] = str(current_path)
        user_data[BROWSE_PAGE_KEY] = 0
        user_data[BROWSE_DIRS_KEY] = subdirs
        await safe_edit(query.message, browser_text, reply_markup=markup)
        return

    if data in (CB_WIN_CANCEL, CB_DIR_CANCEL, CB_SESSION_CANCEL):
        await query.answer("Cancelled")
        clear_browse_state(user_data)
        await safe_edit(query.message, "❌ Operation cancelled.")
        return

    # 3. Directory Browser callbacks
    if data.startswith(CB_DIR_SELECT):
        idx = int(data[len(CB_DIR_SELECT) :])
        curr_str = user_data.get(BROWSE_PATH_KEY, str(Path.home()))
        subdirs = user_data.get(BROWSE_DIRS_KEY, [])
        if 0 <= idx < len(subdirs):
            new_path = Path(curr_str) / subdirs[idx]
            browser_text, markup, new_subs = build_directory_browser(new_path, page=0)
            user_data[BROWSE_PATH_KEY] = str(new_path)
            user_data[BROWSE_PAGE_KEY] = 0
            user_data[BROWSE_DIRS_KEY] = new_subs
            await query.answer()
            await safe_edit(query.message, browser_text, reply_markup=markup)
        return

    if data == CB_DIR_UP:
        curr_str = user_data.get(BROWSE_PATH_KEY, str(Path.home()))
        parent_path = Path(curr_str).parent
        browser_text, markup, new_subs = build_directory_browser(parent_path, page=0)
        user_data[BROWSE_PATH_KEY] = str(parent_path)
        user_data[BROWSE_PAGE_KEY] = 0
        user_data[BROWSE_DIRS_KEY] = new_subs
        await query.answer()
        await safe_edit(query.message, browser_text, reply_markup=markup)
        return

    if data.startswith(CB_DIR_PAGE):
        page = int(data[len(CB_DIR_PAGE) :])
        curr_str = user_data.get(BROWSE_PATH_KEY, str(Path.home()))
        subdirs = user_data.get(BROWSE_DIRS_KEY)
        browser_text, markup, _ = build_directory_browser(
            Path(curr_str), page=page, cached_dirs=subdirs
        )
        user_data[BROWSE_PAGE_KEY] = page
        await query.answer()
        await safe_edit(query.message, browser_text, reply_markup=markup)
        return

    if data == CB_DIR_CONFIRM:
        chosen_dir = user_data.get(BROWSE_PATH_KEY, str(Path.home()))
        recent_sessions = await session_manager.list_recent_sessions_for_cwd(chosen_dir)

        if recent_sessions:
            picker_text, markup = build_session_picker(recent_sessions, chosen_dir)
            user_data[STATE_KEY] = STATE_SELECTING_SESSION
            user_data[SESSIONS_KEY] = recent_sessions
            await query.answer()
            await safe_edit(query.message, picker_text, reply_markup=markup)
            return

        # No recent sessions: create new window immediately
        await _create_and_bind_window(
            query, context, chosen_dir, thread_id, conversation_id=None
        )
        return

    # 4. Session Picker callbacks
    if data.startswith(CB_SESSION_SELECT):
        idx = int(data[len(CB_SESSION_SELECT) :])
        sessions = user_data.get(SESSIONS_KEY, [])
        chosen_dir = user_data.get(BROWSE_PATH_KEY, str(Path.home()))
        if 0 <= idx < len(sessions):
            target_cid = sessions[idx].session_id
            await _create_and_bind_window(
                query, context, chosen_dir, thread_id, conversation_id=target_cid
            )
        return

    if data == CB_SESSION_NEW:
        chosen_dir = user_data.get(BROWSE_PATH_KEY, str(Path.home()))
        await _create_and_bind_window(
            query, context, chosen_dir, thread_id, conversation_id=None
        )
        return

    # 5. Screenshot controls
    if data.startswith(CB_KEYS_PREFIX):
        payload = data[len(CB_KEYS_PREFIX) :]
        key_name, wid = payload.split(":", 1)
        key_trans = {
            "up": ("Up", False),
            "down": ("Down", False),
            "left": ("Left", False),
            "right": ("Right", False),
            "enter": ("Enter", False),
            "esc": ("Escape", False),
            "space": ("Space", False),
            "tab": ("Tab", False),
            "y": ("y", True),
            "n": ("n", True),
        }
        if key_name in key_trans:
            k, enter = key_trans[key_name]
            await tmux_manager.send_keys(wid, k, enter=enter, literal=False)
            await query.answer(f"Sent {key_name}")

        # Update screenshot
        pane_text = await tmux_manager.capture_pane(wid, start_line=-40, with_ansi=True)
        img_bytes = await text_to_image_async(pane_text, font_size=15, with_ansi=True)
        try:
            import io
            from telegram import InputMediaPhoto

            bio = io.BytesIO(img_bytes)
            bio.name = "screenshot.png"
            await query.edit_message_media(
                media=InputMediaPhoto(media=bio),
                reply_markup=_build_screenshot_keyboard(wid),
            )
        except Exception:
            pass
        return

    if data.startswith(CB_SCREENSHOT_REFRESH):
        wid = data[len(CB_SCREENSHOT_REFRESH) :]
        await query.answer("Refreshed")
        pane_text = await tmux_manager.capture_pane(wid, start_line=-40, with_ansi=True)
        img_bytes = await text_to_image_async(pane_text, font_size=15, with_ansi=True)
        try:
            import io
            from telegram import InputMediaPhoto

            bio = io.BytesIO(img_bytes)
            bio.name = "screenshot.png"
            await query.edit_message_media(
                media=InputMediaPhoto(media=bio),
                reply_markup=_build_screenshot_keyboard(wid),
            )
        except Exception:
            pass
        return

    # 6. History pagination
    if data.startswith((CB_HISTORY_PREV, CB_HISTORY_NEXT)):
        prefix = (
            CB_HISTORY_PREV if data.startswith(CB_HISTORY_PREV) else CB_HISTORY_NEXT
        )
        payload = data[len(prefix) :]
        page_str, wid = payload.split(":", 1)
        page_idx = int(page_str)
        await query.answer()
        chat_id = session_manager.resolve_chat_id(user.id, thread_id)
        await send_history(
            bot=context.bot,
            chat_id=chat_id,
            window_id=wid,
            page_index=page_idx,
            message_to_edit=query.message,
            thread_id=thread_id,
        )
        return

    # 7. Interactive prompt buttons (aq:)
    if data.startswith("aq:"):
        action = data[3:].split(":", 1)[0]
        wid = data[3:].split(":", 1)[1]
        await handle_interactive_callback(update, context, action, wid)
        return


async def _create_and_bind_window(
    query: Any,
    context: ContextTypes.DEFAULT_TYPE,
    chosen_dir: str,
    thread_id: int | None,
    conversation_id: str | None = None,
) -> None:
    """Helper to launch new tmux window and bind topic."""
    if thread_id is None:
        await query.answer("Error: no thread_id")
        return

    window_name = Path(chosen_dir).name or "workspace"
    await query.answer(f"Launching {window_name}...")
    await safe_edit(query.message, f"⏳ Starting Antigravity in `{chosen_dir}`...")

    success, msg, final_name, wid = await tmux_manager.create_window(
        window_name=window_name,
        path=chosen_dir,
        start_agy=True,
        conversation_id=conversation_id,
    )

    if not success or not wid:
        await safe_edit(query.message, f"❌ Failed to start window: {msg}")
        clear_browse_state(context.user_data)
        return

    session_manager.bind_thread(thread_id, wid)
    session_manager.set_window_display_name(wid, final_name)

    # Auto-rename Telegram topic if enabled
    if config.auto_rename_topics:
        try:
            chat_id = session_manager.resolve_chat_id(0, thread_id)
            await context.bot.edit_forum_topic(
                chat_id=chat_id,
                message_thread_id=thread_id,
                name=final_name,
            )
        except Exception:
            pass

    await safe_edit(
        query.message,
        f"✅ Topic bound to window *{final_name}* (`{wid}`)\n"
        f"Workspace: `{chosen_dir}`\n"
        f"Mode: `accept-edits` (autonomous)",
    )

    # Forward any pending initial user message
    user_data = context.user_data if context.user_data is not None else {}
    pending_text = user_data.pop("_pending_thread_text", None)
    if pending_text:
        await tmux_manager.send_keys(wid, pending_text, enter=True)

    clear_browse_state(user_data)


# --- Outbound Message Dispatcher from Transcript ---


async def handle_new_message(msg: NewMessage, bot: Bot) -> None:
    """Callback for new transcript messages from session_monitor."""
    # Find which window matches this session_id
    target_wid = None
    for wid, ws in session_manager.window_states.items():
        if ws.session_id == msg.session_id:
            target_wid = wid
            break

    if not target_wid:
        # Check session_map.json
        if config.session_map_file.exists():
            try:
                import json

                content = config.session_map_file.read_text(encoding="utf-8")
                if content.strip():
                    smap = json.loads(content)
                    for key, val in smap.items():
                        if val.get("session_id") == msg.session_id:
                            target_wid = key.split(":")[-1]
                            break
            except Exception:
                pass

    if not target_wid:
        return

    thread_id = session_manager.get_thread_for_window(target_wid)
    parts = build_response_parts(
        text=msg.text,
        is_complete=msg.is_complete,
        content_type=msg.content_type,
        role=msg.role,
    )

    if not parts and not msg.image_data and not msg.document_data:
        return

    # Deliver to user queue
    await enqueue_content_message(
        bot=bot,
        user_id=0,
        window_id=target_wid,
        parts=parts,
        content_type=msg.content_type,
        thread_id=thread_id,
        image_data=msg.image_data,
        document_data=msg.document_data,
    )


# --- Lifecycle & App Factory ---


async def post_init(application: Application) -> None:
    """Post initialization hook: register commands, restore state, start polling."""
    # Set menu commands in Telegram
    commands = [
        BotCommand("start", "Welcome and help"),
        BotCommand("screenshot", "Capture terminal screenshot"),
        BotCommand("history", "View conversation history"),
        BotCommand("artifacts", "Download all session artifacts"),
        BotCommand("esc", "Interrupt current turn (Escape)"),
        BotCommand("clear", "Clear conversation context"),
        BotCommand("usage", "Session statistics"),
        BotCommand("unbind", "Unbind topic from window"),
        BotCommand("kill", "Kill window and unbind topic"),
    ]
    try:
        await application.bot.set_my_commands(commands)
    except Exception as e:
        logger.warning("Could not set bot commands: %s", e)

    # Sync persisted state against running tmux windows
    await session_manager.resolve_stale_ids()

    # Hook message monitor callback
    session_monitor.set_message_callback(
        lambda msg: handle_new_message(msg, application.bot)
    )
    await session_monitor.start()

    # Start terminal status polling loop
    asyncio.create_task(status_poll_loop(application.bot))
    logger.info("agybot post_init complete")


def create_bot() -> Application:
    """Create and configure python-telegram-bot Application."""
    rate_limiter = AIORateLimiter(max_retries=5)
    app = (
        Application.builder()
        .token(config.telegram_bot_token)
        .rate_limiter(rate_limiter)
        .post_init(post_init)
        .build()
    )

    # Commands
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("esc", esc_command))
    app.add_handler(CommandHandler("unbind", unbind_command))
    app.add_handler(CommandHandler("kill", kill_command))
    app.add_handler(CommandHandler("clear", clear_command))
    app.add_handler(CommandHandler("usage", usage_command))
    app.add_handler(CommandHandler("artifacts", artifacts_command))
    app.add_handler(CommandHandler("artifact", artifacts_command))
    app.add_handler(CommandHandler("screenshot", screenshot_command))
    app.add_handler(CommandHandler("history", history_command))

    # Callbacks
    app.add_handler(CallbackQueryHandler(callback_handler))

    # Inbound messages
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_message)
    )

    return app
