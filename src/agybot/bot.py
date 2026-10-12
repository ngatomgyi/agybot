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
import re
from pathlib import Path
from typing import Any

from telegram import (
    Bot,
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.error import BadRequest
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
    CB_SCREENSHOT_CLOSE,
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
from .handlers.inbound_queue import (
    clear_inbound_queue,
    get_queued_prompts,
    is_thread_busy,
    queue_inbound_prompt,
)
from .handlers.interactive_ui import handle_interactive_callback
from .handlers.message_queue import (
    cleanup_status_for_thread,
    enqueue_content_message,
    enqueue_status_update,
    set_active_tool,
)
from .handlers.message_sender import (
    safe_edit,
    safe_reply,
    send_document,
    send_photo,
)
from .handlers.response_builder import build_response_parts
from .handlers.status_polling import get_action_icon, status_poll_loop
from .caveman import (
    is_caveman_rule_active,
    is_caveman_skill_installed,
    set_caveman_rule,
    sync_caveman_configuration,
)
from .screenshot import text_to_image_async
from .session import (
    get_live_session_model_info,
    get_session_token_usage,
    session_manager,
)
from .session_monitor import NewMessage, session_monitor
from .tmux_manager import tmux_manager
from .utils import find_workspace_build_artifacts, format_token_count

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
        "• `/kill` or `/close` - Close/kill tmux window and unbind topic\n"
        "• `/clear` - Clear conversation context\n"
        "• `/usage` - View model & token usage stats\n"
        "• `/caveman` - Toggle Caveman token-saving mode\n"
        "• `/queue` - View queued prompts in topic\n"
        "• `/clearqueue` - Clear queued prompts in topic\n\n"
        "_To get started, open a topic/thread (in direct chat with thread mode on, or in a supergroup forum) and send a message!_"
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

    # Clean up active ephemeral status message, clear tool context, and drain queued tasks
    await cleanup_status_for_thread(context.bot, user.id, thread_id)
    cleared_prompts = clear_inbound_queue(user.id, thread_id)

    success = await tmux_manager.send_interrupt(wid)
    if success:
        notice = "⏹ Sent Escape interrupt to Antigravity."
        if cleared_prompts > 0:
            notice += f" (Cleared {cleared_prompts} queued prompt{'s' if cleared_prompts > 1 else ''}.)"
        await safe_reply(update.message, notice)
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
    """Handle /kill or /close command."""
    user = update.effective_user
    if not user or not is_user_allowed(user.id) or not update.message:
        return

    thread_id = _get_thread_id(update)
    if thread_id is None:
        await safe_reply(
            update.message, "❌ /kill or /close can only be run in a forum topic."
        )
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

    # Reset session_id so auto-resolution immediately picks up the new conversation
    ws = session_manager.get_window_state(wid)
    ws.session_id = ""
    session_manager._save_state()

    await tmux_manager.send_keys(wid, "/clear", enter=True)
    await safe_reply(update.message, "🧹 Sent /clear to Antigravity session.")


async def caveman_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /caveman command to toggle or check Caveman compressed mode."""
    user = update.effective_user
    if not user or not is_user_allowed(user.id) or not update.message:
        return

    thread_id = _get_thread_id(update)
    wid = session_manager.get_window_for_thread(thread_id)
    args = context.args or []
    subcmd = args[0].lower() if args else ""

    if subcmd in ("on", "full"):
        set_caveman_rule(True)
        if wid:
            await tmux_manager.send_keys(wid, "/caveman full", enter=True)
        await safe_reply(
            update.message,
            "🪓 *Caveman Full Mode Enabled*\n"
            "• Output and thoughts compressed to save tokens (~60-70% reduction)\n"
            "• All code and technical accuracy preserved\n"
            "• Active as default rule across sessions",
        )
    elif subcmd in ("off", "stop"):
        set_caveman_rule(False)
        if wid:
            await tmux_manager.send_keys(wid, "stop caveman", enter=True)
        await safe_reply(
            update.message,
            "🗣️ *Caveman Mode Disabled*\n"
            "• Resumed normal detailed response style\n"
            "• Default rule removed",
        )
    elif subcmd in ("lite", "ultra", "wenyan"):
        if wid:
            await tmux_manager.send_keys(wid, f"/caveman {subcmd}", enter=True)
        await safe_reply(
            update.message,
            f"🪓 Sent `/caveman {subcmd}` to Antigravity session.",
        )
    else:
        rule_active = is_caveman_rule_active()
        skill_installed = is_caveman_skill_installed()
        status_line = (
            "🟢 Active (full mode)" if rule_active else "⚪ Disabled (normal mode)"
        )
        text = (
            f"🪓 *Caveman Compressed Mode Status*\n\n"
            f"• Status: {status_line}\n"
            f"• Skill Installed: `{'Yes' if skill_installed else 'No'}`\n"
            f"• Default Config: `{config.caveman_default_mode}`\n\n"
            f"*Commands:*\n"
            f"• `/caveman on` or `/caveman full` - Enable full compression\n"
            f"• `/caveman off` - Disable caveman mode\n"
            f"• `/caveman lite` - Moderate compression\n"
            f"• `/caveman ultra` - Maximum compression"
        )
        await safe_reply(update.message, text)


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
    if not session:
        await safe_reply(update.message, "ℹ️ No session active yet.")
        return

    # Extract live model info and token metrics
    model_info = await get_live_session_model_info(wid)
    tokens = get_session_token_usage(session.file_path)

    model_name = model_info.get("model", "Default")
    effort = model_info.get("effort", "")
    if effort and f"({effort})" not in model_name.lower():
        model_display = f"{model_name} ({effort.capitalize()})"
    else:
        model_display = model_name

    mode_display = model_info.get("agent_mode", "accept-edits")

    lines = [
        "📊 *Session & Model Usage*",
        f"• *Model:* `{model_display}`",
        f"• *Mode:* `{mode_display}`",
        f"• *Input Tokens:* {format_token_count(tokens['input_tokens'])}",
        f"• *Output Tokens:* {format_token_count(tokens['output_tokens'])}",
        f"• *Cached Tokens:* {format_token_count(tokens['cache_read_tokens'])}",
        f"• *Total Tokens:* {format_token_count(tokens['total_tokens'])}",
        f"• *Steps/Messages:* `{session.message_count}`",
    ]
    if session.summary and session.summary != session.session_id[:8]:
        lines.append(f"• *Title:* {session.summary}")
    if session.cwd:
        lines.append(f"• *Workspace:* `{session.cwd}`")
    lines.append(f"• *Session ID:* `{session.session_id}`")

    text = "\n".join(lines)
    await safe_reply(update.message, text)


MODEL_ALIASES: dict[str, str] = {
    "low": "gemini-3.8-flash-low",
    "med": "gemini-3.8-flash-medium",
    "medium": "gemini-3.8-flash-medium",
    "high": "gemini-3.8-flash-high",
    "flash": "gemini-3.8-flash-medium",
    "flash-low": "gemini-3.8-flash-low",
    "flash-med": "gemini-3.8-flash-medium",
    "flash-medium": "gemini-3.8-flash-medium",
    "flash-high": "gemini-3.8-flash-high",
    "3.8": "gemini-3.8-flash-medium",
    "3.8-low": "gemini-3.8-flash-low",
    "3.8-med": "gemini-3.8-flash-medium",
    "3.8-medium": "gemini-3.8-flash-medium",
    "3.8-high": "gemini-3.8-flash-high",
    "pro": "gemini-3.1-pro-high",
    "pro-high": "gemini-3.1-pro-high",
    "pro-low": "gemini-3.1-pro-low",
    "3.1": "gemini-3.1-pro-high",
    "sonnet": "claude-sonnet-4-6",
    "claude": "claude-sonnet-4-6",
    "opus": "claude-opus-4-6-thinking",
}

CB_MODEL_PREFIX = "cbm:"


def _build_model_keyboard(wid: str) -> InlineKeyboardMarkup:
    """Build interactive model & reasoning effort selection keyboard."""
    row1 = [
        InlineKeyboardButton(
            "🟢 Low", callback_data=f"{CB_MODEL_PREFIX}gemini-3.8-flash-low:{wid}"
        ),
        InlineKeyboardButton(
            "🟡 Medium",
            callback_data=f"{CB_MODEL_PREFIX}gemini-3.8-flash-medium:{wid}",
        ),
        InlineKeyboardButton(
            "🔴 High", callback_data=f"{CB_MODEL_PREFIX}gemini-3.8-flash-high:{wid}"
        ),
    ]
    row2 = [
        InlineKeyboardButton(
            "⚡ 3.8 Flash",
            callback_data=f"{CB_MODEL_PREFIX}gemini-3.8-flash-medium:{wid}",
        ),
        InlineKeyboardButton(
            "🧠 3.1 Pro",
            callback_data=f"{CB_MODEL_PREFIX}gemini-3.1-pro-high:{wid}",
        ),
        InlineKeyboardButton(
            "🎭 Claude Sonnet",
            callback_data=f"{CB_MODEL_PREFIX}claude-sonnet-4-6:{wid}",
        ),
    ]
    return InlineKeyboardMarkup([row1, row2])


async def model_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /model, /models, or /effort command."""
    user = update.effective_user
    if not user or not is_user_allowed(user.id) or not update.message:
        return

    thread_id = _get_thread_id(update)
    wid = session_manager.get_window_for_thread(thread_id)
    if not wid:
        await safe_reply(update.message, "❌ No window bound to this topic.")
        return

    args = context.args or []
    if not args:
        info = await get_live_session_model_info(wid)
        model_name = info.get("model", "Gemini 3.8 Flash")
        effort = info.get("effort", "").capitalize() or "Default"
        mode = info.get("agent_mode", "accept-edits")
        text = (
            f"🤖 *Active Model & Reasoning Effort*\n\n"
            f"• *Model:* `{model_name}`\n"
            f"• *Effort:* `{effort}`\n"
            f"• *Mode:* `{mode}`\n\n"
            f"_Switch effort or model below, or type:_\n"
            f"`/model medium` · `/model low` · `/model high` · `/model pro`"
        )
        await safe_reply(
            update.message, text, reply_markup=_build_model_keyboard(wid)
        )
        return

    req = args[0].strip().lower()
    target_model = MODEL_ALIASES.get(req, args[0].strip())

    await tmux_manager.send_keys(wid, f"/model {target_model}", enter=True)
    await asyncio.sleep(0.6)

    info = await get_live_session_model_info(wid)
    model_name = info.get("model", target_model)
    effort = info.get("effort", "").capitalize() or "Default"
    mode = info.get("agent_mode", "accept-edits")

    await safe_reply(
        update.message,
        f"✅ *Model Updated Successfully*\n\n"
        f"• *Model:* `{model_name}`\n"
        f"• *Effort:* `{effort}`\n"
        f"• *Mode:* `{mode}`",
    )


async def queue_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /queue command to view queued prompts in this topic."""
    user = update.effective_user
    if not user or not is_user_allowed(user.id) or not update.message:
        return

    thread_id = _get_thread_id(update)
    if thread_id is None:
        await safe_reply(update.message, "❌ /queue can only be run in a forum topic.")
        return

    prompts = get_queued_prompts(user.id, thread_id)
    if not prompts:
        await safe_reply(
            update.message, "ℹ️ No prompts currently queued for this topic."
        )
        return

    lines = ["⏳ *Queued Prompts:*"]
    for i, p in enumerate(prompts, 1):
        clean_p = p.prompt_text.replace("`", "'")
        if len(clean_p) > 100:
            clean_p = clean_p[:97] + "…"
        lines.append(f"{i}. `{clean_p}`")
    lines.append("\n_Prompts run in order once current task finishes._")
    await safe_reply(update.message, "\n".join(lines))


async def clear_queue_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle /clearqueue command to clear queued prompts in this topic."""
    user = update.effective_user
    if not user or not is_user_allowed(user.id) or not update.message:
        return

    thread_id = _get_thread_id(update)
    if thread_id is None:
        await safe_reply(
            update.message, "❌ /clearqueue can only be run in a forum topic."
        )
        return

    cleared = clear_inbound_queue(user.id, thread_id)
    if cleared:
        await safe_reply(
            update.message,
            f"🧹 Cleared {cleared} queued prompt{'s' if cleared > 1 else ''}.",
        )
    else:
        await safe_reply(update.message, "ℹ️ Queue was already empty.")


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

    # Search workspace for built binaries (.apk, .aab, .ipa)
    cwd = session.cwd or session_manager.get_cwd_for_window(wid)
    oversized: list[tuple[str, float, str]] = []
    if cwd:
        build_files = find_workspace_build_artifacts(cwd)
        seen_names = {name for name, _ in artifacts}
        for bf in build_files:
            if bf.name in seen_names:
                continue
            try:
                sz = bf.stat().st_size
                if sz <= 48 * 1024 * 1024:
                    artifacts.append((bf.name, bf.read_bytes()))
                    seen_names.add(bf.name)
                else:
                    oversized.append((bf.name, sz / (1024 * 1024), str(bf)))
            except Exception as e:
                logger.warning("Error reading build artifact %s: %s", bf, e)

    if not artifacts and not oversized:
        await safe_reply(update.message, "ℹ️ No artifacts found in this session yet.")
        return

    chat_id = session_manager.resolve_chat_id(user.id, thread_id)
    if artifacts:
        await safe_reply(update.message, f"📦 Sending {len(artifacts)} artifact(s)...")
        await send_document(
            bot=context.bot,
            chat_id=chat_id,
            document_data=artifacts,
            message_thread_id=thread_id,
        )

    if oversized:
        text = (
            "⚠️ The following build artifacts exceed Telegram's 50MB bot upload limit:\n"
        )
        for name, sz_mb, path in oversized:
            text += f"• `{name}` ({sz_mb:.1f} MB)\n  📍 `{path}`\n"
        await safe_reply(update.message, text)


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
        InlineKeyboardButton(
            "❌ Close", callback_data=f"{CB_SCREENSHOT_CLOSE}{window_id}"
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


# --- Inbound Messages (Text & Attachments) ---


async def handle_inbound_message(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle regular text messages and file/image attachments sent by user."""
    user = update.effective_user
    if not user or not is_user_allowed(user.id) or not update.message:
        return

    thread_id = _get_thread_id(update)
    chat_id = update.message.chat_id

    # Store group chat_id for routing
    if thread_id is not None:
        session_manager.set_group_chat_id(user.id, thread_id, chat_id)

    msg = update.message
    caption = (msg.caption or "").strip()
    raw_text = (msg.text or "").strip()

    # Topic unbound check first
    if thread_id is None:
        await safe_reply(
            msg,
            "ℹ️ agybot operates inside Telegram Forum topics.\n"
            "Please create a topic and send your message there!",
        )
        return

    wid = session_manager.get_window_for_thread(thread_id)

    # Process attachment if photo or document
    prompt_text = raw_text
    if msg.photo or msg.document:
        if not wid:
            await safe_reply(
                msg, "❌ Please bind this topic to a window before sending files."
            )
            return

        cwd = session_manager.get_cwd_for_window(wid) or str(Path.home())
        attachments_dir = Path(cwd) / "attachments"
        attachments_dir.mkdir(parents=True, exist_ok=True)

        try:
            if msg.photo:
                # Use highest resolution photo
                photo_obj = msg.photo[-1]
                filename = (
                    f"photo_{msg.message_id}_{int(asyncio.get_event_loop().time())}.jpg"
                )
                save_dest = attachments_dir / filename
                tf = await context.bot.get_file(photo_obj.file_id)
                await tf.download_to_drive(save_dest)
                prefix = f"[Attached image: {save_dest.resolve()}]"
                clean_cap = caption.replace("\n", " ").strip()
                prompt_text = f"{prefix} {clean_cap}".strip()
            elif msg.document:
                doc_obj = msg.document
                orig_name = doc_obj.file_name or f"file_{doc_obj.file_id[:8]}"
                safe_name = re.sub(r"[^\w\.-]", "_", orig_name)
                save_dest = attachments_dir / safe_name
                if save_dest.exists():
                    save_dest = (
                        attachments_dir
                        / f"{save_dest.stem}_{msg.message_id}{save_dest.suffix}"
                    )
                tf = await context.bot.get_file(doc_obj.file_id)
                await tf.download_to_drive(save_dest)
                prefix = f"[Attached file: {save_dest.resolve()}]"
                clean_cap = caption.replace("\n", " ").strip()
                prompt_text = f"{prefix} {clean_cap}".strip()
        except BadRequest as e:
            if "file is too big" in str(e).lower():
                await safe_reply(
                    msg, "⚠️ Attachment exceeds Telegram's 20MB bot download limit."
                )
                return
            await safe_reply(msg, f"⚠️ Failed to download attachment: {e}")
            return
        except Exception as e:
            logger.error("Error downloading attachment: %s", e)
            await safe_reply(msg, f"⚠️ Failed to process attachment: {e}")
            return

    if not prompt_text:
        return

    # 1. Topic already bound to a window -> forward prompt or queue if busy
    if wid:
        ws = session_manager.get_window_state(wid)
        if not ws.session_id:
            try:
                await session_manager.auto_resolve_agy_window(wid)
            except Exception as e:
                logger.debug("Failed to auto-resolve agy window in inbound: %s", e)

        if is_thread_busy(user.id, thread_id):
            pos = queue_inbound_prompt(user.id, thread_id, prompt_text)
            clean_preview = prompt_text
            if len(clean_preview) > 100:
                clean_preview = clean_preview[:97] + "…"
            await safe_reply(
                msg,
                f"⏳ *Queued (#{pos})*:\n`{clean_preview}`\n_Will execute automatically once current task completes._",
            )
            return

        await cleanup_status_for_thread(context.bot, user.id, thread_id)
        clean_prompt = prompt_text.replace("\n", " ")
        await tmux_manager.send_keys(wid, clean_prompt, enter=True)
        set_active_tool(user.id, thread_id, "Working…", "")
        await enqueue_status_update(
            context.bot,
            user.id,
            wid,
            "⏳ Working…",
            thread_id,
        )
        return

    # 2. Topic is unbound -> Show Window Picker or Directory Browser
    user_data = context.user_data if context.user_data is not None else {}
    user_data["_pending_thread_id"] = thread_id
    user_data["_pending_thread_text"] = prompt_text

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
    if thread_id is not None and update.effective_chat:
        session_manager.set_group_chat_id(user.id, thread_id, update.effective_chat.id)
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
            session_manager.set_window_display_name(wid, name)
            ws = session_manager.get_window_state(wid)
            ws.cwd = cwd
            ws.window_name = name
            await session_manager.auto_resolve_agy_window(wid)
            await query.answer(f"Attached window {name}")
            await safe_edit(
                query.message, f"🔗 Bound topic to tmux window `{name}` ({wid})"
            )
            # Forward pending text if any
            pending_text = user_data.pop("_pending_thread_text", None)
            if pending_text:
                clean_prompt = pending_text.replace("\n", " ")
                await tmux_manager.send_keys(wid, clean_prompt, enter=True)
                uid = (
                    user.id
                    if user
                    else session_manager.resolve_user_id_for_thread(thread_id)
                )
                if uid:
                    set_active_tool(uid, thread_id, "Working…", "")
                    await enqueue_status_update(
                        context.bot,
                        uid,
                        wid,
                        "⏳ Working…",
                        thread_id,
                    )
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

    if data.startswith(CB_SCREENSHOT_CLOSE):
        await query.answer("Screenshot closed")
        if query.message:
            try:
                await query.message.delete()
            except Exception as e:
                logger.debug("Could not delete screenshot message: %s", e)
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

    # 8. Model & Effort selection buttons (cbm:)
    if data.startswith(CB_MODEL_PREFIX):
        payload = data[len(CB_MODEL_PREFIX) :]
        target_model, target_wid = payload.split(":", 1)
        await query.answer("Switching model...")
        await tmux_manager.send_keys(target_wid, f"/model {target_model}", enter=True)
        await asyncio.sleep(0.6)
        info = await get_live_session_model_info(target_wid)
        model_name = info.get("model", target_model)
        effort = info.get("effort", "").capitalize() or "Default"
        mode = info.get("agent_mode", "accept-edits")
        text = (
            f"✅ *Model Updated Successfully*\n\n"
            f"• *Model:* `{model_name}`\n"
            f"• *Effort:* `{effort}`\n"
            f"• *Mode:* `{mode}`\n\n"
            f"_Switch effort or model below, or type:_\n"
            f"`/model medium` · `/model low` · `/model high` · `/model pro`"
        )
        await safe_edit(
            query.message, text, reply_markup=_build_model_keyboard(target_wid)
        )
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
    ws = session_manager.get_window_state(wid)
    ws.cwd = chosen_dir
    ws.window_name = final_name

    if conversation_id:
        ws.session_id = conversation_id
        session_manager._save_state()
        await session_manager._record_session_map_entry(
            wid, conversation_id, cwd=chosen_dir, window_name=final_name
        )
        t_path = (
            config.agy_brain_path
            / conversation_id
            / ".system_generated"
            / "logs"
            / "transcript.jsonl"
        )
        if t_path.exists():
            session_monitor.state.update_offset(
                conversation_id, t_path, t_path.stat().st_size
            )
            session_monitor.state.save()

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
    user_id = (
        query.from_user.id
        if (query and query.from_user)
        else session_manager.resolve_user_id_for_thread(thread_id)
    )
    bot = context.bot
    clear_browse_state(user_data)

    async def _init_and_forward(
        target_wid: str,
        t_id: int,
        u_id: int,
        p_text: str | None,
        target_cid: str | None,
    ) -> None:
        resolved_cid = target_cid
        for _ in range(30):  # poll up to 15 seconds (30 * 0.5s)
            if resolved_cid:
                break
            await asyncio.sleep(0.5)
            resolved_cid = await session_manager.auto_resolve_agy_window(target_wid)

        if not resolved_cid:
            logger.warning("Could not auto-resolve session for window %s", target_wid)

        if p_text:
            await asyncio.sleep(0.5)
            clean_prompt = p_text.replace("\n", " ")
            await tmux_manager.send_keys(target_wid, clean_prompt, enter=True)
            if u_id:
                set_active_tool(u_id, t_id, "Working…", "")
                await enqueue_status_update(
                    bot,
                    u_id,
                    target_wid,
                    "⏳ Working…",
                    t_id,
                )

    asyncio.create_task(
        _init_and_forward(
            wid,
            thread_id,
            user_id,
            pending_text,
            conversation_id,
        )
    )


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
    user_id = session_manager.resolve_user_id_for_thread(thread_id)

    # Ephemeral live progress for tool execution (in-place status message)
    if msg.content_type == "tool_use":
        if not msg.text:
            return
        raw = msg.text.replace("**", "").strip()
        if "\n" in raw:
            action, detail = raw.split("\n", 1)
            action = action.strip()
            detail = detail.strip()
        else:
            action = raw
            detail = ""

        set_active_tool(user_id, thread_id, action, detail)
        icon = get_action_icon(action)
        status_text = f"{icon} {action}\n{detail}" if detail else f"{icon} {action}"

        await enqueue_status_update(
            bot=bot,
            user_id=user_id,
            window_id=target_wid,
            status_text=status_text,
            thread_id=thread_id,
        )
        return

    if msg.content_type == "thinking":
        set_active_tool(user_id, thread_id, "Thinking…", "")

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
        user_id=user_id,
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
        BotCommand("model", "View or switch model & reasoning effort"),
        BotCommand("usage", "Model and token usage statistics"),
        BotCommand("caveman", "Toggle Caveman compressed mode"),
        BotCommand("unbind", "Unbind topic from window"),
        BotCommand("kill", "Kill window and unbind topic"),
        BotCommand("close", "Close window and unbind topic"),
        BotCommand("queue", "View queued prompts in topic"),
        BotCommand("clearqueue", "Clear queued prompts in topic"),
    ]
    try:
        await application.bot.set_my_commands(commands)
    except Exception as e:
        logger.warning("Could not set bot commands: %s", e)

    # Sync persisted state against running tmux windows
    await session_manager.resolve_stale_ids()

    # Sync Caveman compressed mode skill and rule
    sync_caveman_configuration(config.caveman_default_mode)

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
    rate_limiter = AIORateLimiter(
        overall_max_rate=30,
        overall_time_period=1.0,
        group_max_rate=15,
        group_time_period=60.0,
        max_retries=3,
    )
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
    app.add_handler(CommandHandler(["kill", "close"], kill_command))
    app.add_handler(CommandHandler("clear", clear_command))
    app.add_handler(CommandHandler(["model", "models", "effort"], model_command))
    app.add_handler(CommandHandler("usage", usage_command))
    app.add_handler(CommandHandler("caveman", caveman_command))
    app.add_handler(CommandHandler("queue", queue_command))
    app.add_handler(CommandHandler(["clearqueue", "cq"], clear_queue_command))
    app.add_handler(CommandHandler("artifacts", artifacts_command))
    app.add_handler(CommandHandler("artifact", artifacts_command))
    app.add_handler(CommandHandler("screenshot", screenshot_command))
    app.add_handler(CommandHandler("history", history_command))

    # Callbacks
    app.add_handler(CallbackQueryHandler(callback_handler))

    # Inbound messages (text, photos, documents)
    app.add_handler(
        MessageHandler(
            (filters.TEXT | filters.PHOTO | filters.Document.ALL) & ~filters.COMMAND,
            handle_inbound_message,
        )
    )

    return app
