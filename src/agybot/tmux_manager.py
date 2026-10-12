"""Tmux session and window management via libtmux for Antigravity sessions.

Manages a single tmux session (default: "agybot"):
  - 1 Telegram Topic = 1 Tmux Window = 1 Antigravity Session
  - Asynchronous wrappers around libtmux blocking operations
  - Window creation, teardown, terminal capture, and keystroke forwarding
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path

import libtmux

from .config import SENSITIVE_ENV_VARS, config

logger = logging.getLogger(__name__)


@dataclass
class TmuxWindow:
    """Information about a tmux window."""

    window_id: str
    window_name: str
    cwd: str
    pane_current_command: str = ""


class TmuxManager:
    """Manages tmux windows for Antigravity CLI sessions."""

    def __init__(self, session_name: str | None = None):
        self.session_name = session_name or config.tmux_session_name
        self._server: libtmux.Server | None = None

    @property
    def server(self) -> libtmux.Server:
        """Get or initialize the tmux server connection."""
        if self._server is None:
            self._server = libtmux.Server()
        return self._server

    def get_session(self) -> libtmux.Session | None:
        """Get the agybot tmux session if it exists."""
        try:
            return self.server.sessions.get(session_name=self.session_name)
        except Exception:
            return None

    def get_or_create_session(self) -> libtmux.Session:
        """Get the existing session or create a new one."""
        session = self.get_session()
        if session:
            self._scrub_session_env(session)
            return session

        session = self.server.new_session(
            session_name=self.session_name,
            start_directory=str(Path.home()),
        )
        if session.windows:
            session.windows[0].rename_window(config.tmux_main_window_name)
        self._scrub_session_env(session)
        return session

    @staticmethod
    def _scrub_session_env(session: libtmux.Session) -> None:
        """Scrub sensitive environment variables from tmux session environment."""
        for var in SENSITIVE_ENV_VARS:
            try:
                session.unset_environment(var)
            except Exception:
                pass

    async def list_windows(self) -> list[TmuxWindow]:
        """List all windows in the agybot tmux session."""

        def _list() -> list[TmuxWindow]:
            session = self.get_session()
            if not session:
                return []
            results = []
            for w in session.windows:
                pane = w.active_pane
                cwd = pane.pane_current_path if pane else str(Path.home())
                cmd = pane.pane_current_command if pane else ""
                results.append(
                    TmuxWindow(
                        window_id=w.window_id or "",
                        window_name=w.window_name or "",
                        cwd=cwd or str(Path.home()),
                        pane_current_command=cmd or "",
                    )
                )
            return results

        return await asyncio.to_thread(_list)

    async def find_window_by_id(self, window_id: str) -> TmuxWindow | None:
        """Find a window by its tmux window_id (e.g. '@1')."""
        windows = await self.list_windows()
        for w in windows:
            if w.window_id == window_id:
                return w
        return None

    async def find_window_by_name(self, window_name: str) -> TmuxWindow | None:
        """Find a window by its window name."""
        windows = await self.list_windows()
        for w in windows:
            if w.window_name == window_name:
                return w
        return None

    async def capture_pane(
        self, window_id: str, start_line: int = -100, with_ansi: bool = False
    ) -> str:
        """Capture terminal output from the active pane of window_id."""

        def _capture() -> str:
            session = self.get_session()
            if not session:
                return ""
            try:
                w = session.windows.get(window_id=window_id)
            except Exception:
                return ""
            if not w or not w.active_pane:
                return ""
            try:
                lines = w.active_pane.cmd(
                    "capture-pane",
                    "-p",
                    "-e" if with_ansi else "-J",
                    "-S",
                    str(start_line),
                ).stdout
                return "\n".join(lines)
            except Exception as e:
                logger.debug("Failed to capture pane %s: %s", window_id, e)
                return ""

        return await asyncio.to_thread(_capture)

    async def send_keys(
        self,
        window_id: str,
        text: str,
        enter: bool = True,
        literal: bool = False,
    ) -> bool:
        """Send keystrokes to the active pane of window_id."""

        def _send() -> bool:
            session = self.get_session()
            if not session:
                return False
            try:
                w = session.windows.get(window_id=window_id)
            except Exception:
                return False
            if not w or not w.active_pane:
                return False
            try:
                w.active_pane.send_keys(text, enter=enter, literal=literal)
                return True
            except Exception as e:
                logger.error("Failed to send keys to %s: %s", window_id, e)
                return False

        return await asyncio.to_thread(_send)

    async def send_interrupt(self, window_id: str) -> bool:
        """Send Escape key to interrupt the current agent turn."""
        return await self.send_keys(window_id, "Escape", enter=False, literal=False)

    async def create_window(
        self,
        window_name: str,
        path: str,
        start_agy: bool = True,
        conversation_id: str | None = None,
    ) -> tuple[bool, str, str, str]:
        """Create a new tmux window and optionally launch Antigravity.

        Returns:
            (success, message, final_window_name, window_id)
        """

        def _create() -> tuple[bool, str, str, str]:
            session = self.get_or_create_session()
            dir_path = Path(path).resolve()
            if not dir_path.is_dir():
                return False, f"Directory does not exist: {path}", "", ""

            # Ensure unique window name
            existing_names = {w.window_name for w in session.windows}
            final_name = window_name
            counter = 2
            while final_name in existing_names:
                final_name = f"{window_name}-{counter}"
                counter += 1

            try:
                window = session.new_window(
                    window_name=final_name,
                    start_directory=str(dir_path),
                    attach=False,
                )
                wid = window.window_id or ""
                window.set_window_option("allow-rename", "off")

                if start_agy and window.active_pane:
                    cmd = config.agy_command
                    if conversation_id:
                        cmd = f"{cmd} --conversation {conversation_id}"
                    window.active_pane.send_keys(cmd, enter=True)

                logger.info(
                    "Created window '%s' (id=%s) at %s",
                    final_name,
                    wid,
                    path,
                )
                return True, f"Created window '{final_name}'", final_name, wid
            except Exception as e:
                logger.error("Failed to create tmux window: %s", e)
                return False, f"Error creating window: {e}", "", ""

        return await asyncio.to_thread(_create)

    async def kill_window(self, window_id: str) -> bool:
        """Kill a tmux window by window_id."""

        def _kill() -> bool:
            session = self.get_session()
            if not session:
                return False
            try:
                w = session.windows.get(window_id=window_id)
            except Exception:
                return False
            if not w:
                return False
            try:
                w.kill_window()
                logger.info("Killed tmux window %s", window_id)
                return True
            except Exception as e:
                logger.error("Failed to kill window %s: %s", window_id, e)
                return False

        return await asyncio.to_thread(_kill)


tmux_manager = TmuxManager()
