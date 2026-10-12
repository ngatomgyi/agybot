"""Directory browser and window/session picker UI for agybot.

Provides inline keyboards for:
  - Window Picker: Pick existing unbound tmux windows in the agybot session
  - Directory Browser: Browse filesystem to pick workspace directory
  - Session Picker: Pick recent Antigravity conversations in the selected workspace
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from ..config import config
from ..session import AgySession
from .callback_data import (
    CB_DIR_CANCEL,
    CB_DIR_CONFIRM,
    CB_DIR_PAGE,
    CB_DIR_SELECT,
    CB_DIR_UP,
    CB_SESSION_CANCEL,
    CB_SESSION_NEW,
    CB_SESSION_SELECT,
    CB_WIN_BIND,
    CB_WIN_CANCEL,
    CB_WIN_NEW,
)

DIRS_PER_PAGE = 6

STATE_KEY = "state"
STATE_BROWSING_DIRECTORY = "browsing_directory"
STATE_SELECTING_WINDOW = "selecting_window"
STATE_SELECTING_SESSION = "selecting_session"

BROWSE_PATH_KEY = "browse_path"
BROWSE_PAGE_KEY = "browse_page"
BROWSE_DIRS_KEY = "browse_dirs"
UNBOUND_WINDOWS_KEY = "unbound_windows"
SESSIONS_KEY = "cached_sessions"


def clear_browse_state(user_data: dict[str, Any] | None) -> None:
    """Clear directory browser state keys."""
    if user_data is not None:
        user_data.pop(STATE_KEY, None)
        user_data.pop(BROWSE_PATH_KEY, None)
        user_data.pop(BROWSE_PAGE_KEY, None)
        user_data.pop(BROWSE_DIRS_KEY, None)
        user_data.pop(UNBOUND_WINDOWS_KEY, None)
        user_data.pop(SESSIONS_KEY, None)


def build_window_picker(
    unbound_windows: list[tuple[str, str, str]],  # (window_id, name, cwd)
) -> tuple[str, InlineKeyboardMarkup]:
    """Build Window Picker UI for unbound tmux windows."""
    text = (
        "🔍 *Found existing tmux windows in agybot session*\n\n"
        "Select a window to attach to this topic, or create a new one:"
    )
    buttons = []
    for idx, (wid, name, cwd) in enumerate(unbound_windows):
        short_cwd = cwd.replace(str(Path.home()), "~")
        buttons.append(
            [
                InlineKeyboardButton(
                    f"{idx + 1}. {name} ({short_cwd})",
                    callback_data=f"{CB_WIN_BIND}{idx}",
                )
            ]
        )

    buttons.append(
        [
            InlineKeyboardButton("➕ Create New Window", callback_data=CB_WIN_NEW),
            InlineKeyboardButton("❌ Cancel", callback_data=CB_WIN_CANCEL),
        ]
    )
    return text, InlineKeyboardMarkup(buttons)


def build_directory_browser(
    current_path: Path,
    page: int = 0,
    cached_dirs: list[str] | None = None,
) -> tuple[str, InlineKeyboardMarkup, list[str]]:
    """Build filesystem Directory Browser UI."""
    if cached_dirs is None:
        try:
            subdirs = sorted(
                [
                    d.name
                    for d in current_path.iterdir()
                    if d.is_dir()
                    and (config.show_hidden_dirs or not d.name.startswith("."))
                ]
            )
        except (PermissionError, OSError):
            subdirs = []
    else:
        subdirs = cached_dirs

    total_dirs = len(subdirs)
    total_pages = max(1, (total_dirs + DIRS_PER_PAGE - 1) // DIRS_PER_PAGE)
    page = max(0, min(page, total_pages - 1))

    start_idx = page * DIRS_PER_PAGE
    end_idx = min(start_idx + DIRS_PER_PAGE, total_dirs)
    page_dirs = subdirs[start_idx:end_idx]

    display_path = str(current_path).replace(str(Path.home()), "~")
    text = (
        f"📂 *Select Workspace Directory*\n"
        f"Current: `{display_path}`\n\n"
        f"Choose a subfolder or confirm this directory:"
    )

    buttons: list[list[InlineKeyboardButton]] = []

    # Subdirectory buttons (2 per row)
    row: list[InlineKeyboardButton] = []
    for idx_in_page, d_name in enumerate(page_dirs):
        global_idx = start_idx + idx_in_page
        row.append(
            InlineKeyboardButton(
                f"📁 {d_name}",
                callback_data=f"{CB_DIR_SELECT}{global_idx}",
            )
        )
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)

    # Pagination controls if multiple pages
    if total_pages > 1:
        nav_row: list[InlineKeyboardButton] = []
        if page > 0:
            nav_row.append(
                InlineKeyboardButton("◀ Prev", callback_data=f"{CB_DIR_PAGE}{page - 1}")
            )
        nav_row.append(
            InlineKeyboardButton(f"{page + 1}/{total_pages}", callback_data="noop")
        )
        if page < total_pages - 1:
            nav_row.append(
                InlineKeyboardButton("Next ▶", callback_data=f"{CB_DIR_PAGE}{page + 1}")
            )
        buttons.append(nav_row)

    # Action buttons: Up, Confirm, Cancel
    actions: list[InlineKeyboardButton] = []
    if current_path != current_path.parent:
        actions.append(InlineKeyboardButton("⬆️ Up", callback_data=CB_DIR_UP))
    actions.append(
        InlineKeyboardButton("✅ Select This Folder", callback_data=CB_DIR_CONFIRM)
    )
    actions.append(InlineKeyboardButton("❌ Cancel", callback_data=CB_DIR_CANCEL))
    buttons.append(actions)

    return text, InlineKeyboardMarkup(buttons), subdirs


def build_session_picker(
    sessions: list[AgySession],
    cwd: str,
) -> tuple[str, InlineKeyboardMarkup]:
    """Build Session Picker UI for existing Antigravity sessions in workspace."""
    short_cwd = cwd.replace(str(Path.home()), "~")
    text = (
        f"🚀 *Recent Antigravity Sessions in `{short_cwd}`*\n\n"
        f"Select an existing session to resume, or start fresh:"
    )
    buttons = []
    for idx, s in enumerate(sessions[:6]):
        title = s.summary[:30] + ("…" if len(s.summary) > 30 else "")
        buttons.append(
            [
                InlineKeyboardButton(
                    f"{idx + 1}. {title} ({s.message_count} msgs)",
                    callback_data=f"{CB_SESSION_SELECT}{idx}",
                )
            ]
        )

    buttons.append(
        [
            InlineKeyboardButton("✨ Start New Session", callback_data=CB_SESSION_NEW),
            InlineKeyboardButton("❌ Cancel", callback_data=CB_SESSION_CANCEL),
        ]
    )
    return text, InlineKeyboardMarkup(buttons)
