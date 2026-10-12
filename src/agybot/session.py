"""Session state and Antigravity conversation resolution.

Responsibilities:
  - Topic <-> Window binding (1 Telegram Topic = 1 Tmux Window = 1 Session)
  - Supergroup chat_id resolution for Forum topics
  - Direct auto-detection of Antigravity conversation IDs from tmux processes
  - Querying Antigravity SQLite database (~/.gemini/antigravity-cli/conversation_summaries.db)
    for conversation titles and recent sessions
"""

from __future__ import annotations

import asyncio
import glob
import json
import logging
import os
import re
import sqlite3
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import aiofiles

from .config import config
from .tmux_manager import tmux_manager
from .utils import atomic_write_json

logger = logging.getLogger(__name__)


@dataclass
class AgySession:
    """Represents an Antigravity conversation session."""

    session_id: str
    summary: str
    message_count: int
    file_path: str
    cwd: str = ""


@dataclass
class WindowState:
    """Represents the state of a tmux window."""

    window_id: str
    session_id: str
    cwd: str
    window_name: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> WindowState:
        return cls(
            window_id=data.get("window_id", ""),
            session_id=data.get("session_id", ""),
            cwd=data.get("cwd", ""),
            window_name=data.get("window_name", ""),
        )


class SessionManager:
    """Manages thread bindings, window states, and Antigravity conversation mappings."""

    def __init__(self) -> None:
        # thread_id -> window_id (e.g. 1024 -> "@1")
        self.thread_bindings: dict[int, str] = {}
        # "{user_id}:{thread_id}" -> group chat_id (-100...)
        self.group_chat_ids: dict[str, int] = {}
        # window_id -> WindowState
        self.window_states: dict[str, WindowState] = {}
        # window_id -> window display name
        self.window_display_names: dict[str, str] = {}
        self._load_state()

    def _load_state(self) -> None:
        """Load persistent thread bindings and window states from state.json."""
        if not config.state_file.exists():
            return
        try:
            data = json.loads(config.state_file.read_text(encoding="utf-8"))
            raw_bindings = data.get("thread_bindings", {})
            self.thread_bindings = {int(k): v for k, v in raw_bindings.items()}
            self.group_chat_ids = data.get("group_chat_ids", {})
            raw_states = data.get("window_states", {})
            self.window_states = {
                k: WindowState.from_dict(v) for k, v in raw_states.items()
            }
            self.window_display_names = data.get("window_display_names", {})
            logger.debug(
                "Loaded state: %d bindings, %d window states",
                len(self.thread_bindings),
                len(self.window_states),
            )
        except Exception as e:
            logger.warning("Failed to load state: %s", e)

    def _save_state(self) -> None:
        """Save state to state.json atomically."""
        try:
            data = {
                "thread_bindings": {str(k): v for k, v in self.thread_bindings.items()},
                "group_chat_ids": self.group_chat_ids,
                "window_states": {
                    k: v.to_dict() for k, v in self.window_states.items()
                },
                "window_display_names": self.window_display_names,
            }
            atomic_write_json(config.state_file, data)
        except Exception as e:
            logger.warning("Failed to save state: %s", e)

    # --- Topic / Group chat ID resolution ---

    def set_group_chat_id(
        self, user_id: int, thread_id: int | None, chat_id: int
    ) -> None:
        """Store Telegram supergroup chat_id for a user + thread pair."""
        tid = thread_id or 0
        key = f"{user_id}:{tid}"
        changed = False
        if self.group_chat_ids.get(key) != chat_id:
            self.group_chat_ids[key] = chat_id
            changed = True
        if tid != 0:
            zero_key = f"0:{tid}"
            if self.group_chat_ids.get(zero_key) != chat_id:
                self.group_chat_ids[zero_key] = chat_id
                changed = True
        if changed:
            self._save_state()

    def resolve_chat_id(self, user_id: int, thread_id: int | None = None) -> int:
        """Resolve destination chat_id (group chat_id or private user_id)."""
        if thread_id is not None:
            # 1. Exact match for user_id:thread_id
            key = f"{user_id}:{thread_id}"
            group_id = self.group_chat_ids.get(key)
            if group_id is not None:
                return group_id
            # 2. Check any entry associated with this thread_id
            for k, gid in self.group_chat_ids.items():
                if k.endswith(f":{thread_id}"):
                    return gid
        if user_id != 0:
            return user_id
        if config.allowed_users:
            return config.allowed_users[0]
        return 0

    def resolve_user_id_for_thread(self, thread_id: int | None = None) -> int:
        """Resolve user_id associated with a thread_id."""
        if thread_id is not None:
            for k in self.group_chat_ids:
                parts = k.split(":")
                if len(parts) == 2 and parts[1] == str(thread_id):
                    try:
                        uid = int(parts[0])
                        if uid != 0:
                            return uid
                    except ValueError:
                        pass
        if config.allowed_users:
            return config.allowed_users[0]
        return 0

    # --- Window / Thread bindings ---

    def bind_thread(self, thread_id: int, window_id: str) -> None:
        """Bind a Telegram topic thread_id to a tmux window_id."""
        self.thread_bindings[thread_id] = window_id
        self._save_state()
        logger.info("Bound thread %d to window %s", thread_id, window_id)

    def unbind_thread(self, thread_id: int) -> str | None:
        """Unbind a Telegram topic thread_id."""
        wid = self.thread_bindings.pop(thread_id, None)
        if wid:
            self._save_state()
            logger.info("Unbound thread %d (was %s)", thread_id, wid)
        return wid

    def get_window_for_thread(self, thread_id: int | None) -> str | None:
        """Get the window_id bound to thread_id."""
        if thread_id is None:
            return None
        return self.thread_bindings.get(thread_id)

    def resolve_window_for_thread(
        self, user_id: int, thread_id: int | None
    ) -> str | None:
        """Resolve window_id for a user and topic."""
        return self.get_window_for_thread(thread_id)

    def get_thread_for_window(self, window_id: str) -> int | None:
        """Find the thread_id bound to a window_id."""
        for tid, wid in self.thread_bindings.items():
            if wid == window_id:
                return tid
        return None

    def get_cwd_for_window(self, window_id: str) -> str | None:
        """Get working directory for a window_id."""
        ws = self.window_states.get(window_id)
        if ws and ws.cwd:
            return ws.cwd
        return None

    def get_cwd_for_session(self, session_id: str) -> str | None:
        """Find working directory associated with a session ID."""
        for ws in self.window_states.values():
            if ws.session_id == session_id and ws.cwd:
                return ws.cwd
        if config.session_map_file.exists():
            try:
                content = config.session_map_file.read_text(encoding="utf-8")
                if content.strip():
                    smap = json.loads(content)
                    for val in smap.values():
                        if val.get("session_id") == session_id and val.get("cwd"):
                            return val.get("cwd")
            except Exception:
                pass
        return None

    def get_window_state(self, window_id: str) -> WindowState:
        """Get or initialize WindowState for window_id."""
        if window_id not in self.window_states:
            self.window_states[window_id] = WindowState(
                window_id=window_id,
                session_id="",
                cwd="",
            )
        return self.window_states[window_id]

    def set_window_display_name(self, window_id: str, new_name: str) -> None:
        """Update window display name."""
        self.window_display_names[window_id] = new_name
        if window_id in self.window_states:
            self.window_states[window_id].window_name = new_name
        self._save_state()

    def get_window_display_name(self, window_id: str) -> str:
        """Get display name for a window."""
        return self.window_display_names.get(window_id, window_id)

    # --- Antigravity auto-resolution ---

    async def auto_resolve_agy_window(self, window_id: str) -> str | None:
        """Auto-detect the Antigravity conversation running in window_id."""
        try:
            window = await tmux_manager.find_window_by_id(window_id)
            if not window:
                return None

            target = f"{config.tmux_session_name}:{window_id}"
            res = await asyncio.to_thread(
                subprocess.run,
                ["tmux", "list-panes", "-t", target, "-F", "#{pane_pid}"],
                capture_output=True,
                text=True,
            )
            pane_pids = res.stdout.strip().splitlines()
            if not pane_pids:
                return None
            pane_pid = pane_pids[0].strip()

            def _find_agy_conv(pid_str: str) -> str | None:
                descendants = []
                queue = [pid_str]
                while queue:
                    curr = queue.pop(0)
                    try:
                        with open(f"/proc/{curr}/task/{curr}/children", "r") as f:
                            ch = f.read().split()
                        descendants.extend(ch)
                        queue.extend(ch)
                    except Exception:
                        pass

                for cpid in descendants:
                    try:
                        with open(f"/proc/{cpid}/cmdline", "rb") as f:
                            cmd = f.read().decode("utf-8", errors="ignore")
                        if "agy" not in cmd:
                            continue

                        # 1. Check open file descriptors for active presence lock file or CLI log file
                        fd_dir = f"/proc/{cpid}/fd"
                        log_path = None
                        if os.path.exists(fd_dir):
                            for fd in os.listdir(fd_dir):
                                try:
                                    target = os.readlink(os.path.join(fd_dir, fd))
                                    m = re.search(
                                        r"presence/([0-9a-f-]{36})\.lock", target
                                    )
                                    if m:
                                        return m.group(1)
                                    if "log/cli-" in target and os.path.exists(target):
                                        log_path = target
                                except OSError:
                                    pass

                        if log_path:
                            with open(log_path, "r", errors="ignore") as f:
                                lines = f.readlines()[-300:]
                            for line in reversed(lines):
                                m = re.search(r"to conversation ([0-9a-f-]{36})", line)
                                if m:
                                    return m.group(1)
                                if "Starting conversation update stream for " in line:
                                    return line.split(
                                        "Starting conversation update stream for "
                                    )[1].strip()
                                if "Streaming conversation " in line:
                                    return (
                                        line.split("Streaming conversation ")[1]
                                        .strip()
                                        .split()[0]
                                    )
                                if "GetConversationDetail: found conversation " in line:
                                    return (
                                        line.split(
                                            "GetConversationDetail: found conversation "
                                        )[1]
                                        .strip()
                                        .split()[0]
                                    )

                        # 2. Check crashes folder
                        crashes = glob.glob(
                            f"{config.agy_crashes_dir}/crash_{cpid}_*.log"
                        )
                        if crashes:
                            return (
                                os.path.basename(crashes[0])
                                .split("_")[2]
                                .replace(".log", "")
                            )
                    except Exception:
                        pass

                # 3. Fallback: check history.jsonl
                if config.agy_history_file.exists():
                    try:
                        with open(config.agy_history_file, "r", errors="ignore") as f:
                            lines = f.readlines()[-30:]
                        norm_cwd = str(Path(window.cwd).resolve())
                        for line in reversed(lines):
                            try:
                                d = json.loads(line)
                                wspace = (
                                    str(Path(d.get("workspace", "")).resolve())
                                    if d.get("workspace")
                                    else ""
                                )
                                cid = d.get("conversationId")
                                if cid and wspace == norm_cwd:
                                    return cid
                            except Exception:
                                pass
                    except Exception:
                        pass
                return None

            cid = await asyncio.to_thread(_find_agy_conv, pane_pid)
            if cid:
                ws = self.get_window_state(window_id)
                if ws.session_id != cid or ws.cwd != window.cwd:
                    ws.session_id = cid
                    ws.cwd = window.cwd
                    ws.window_name = window.window_name
                    self._save_state()
                    await self._record_session_map_entry(
                        window_id, cid, cwd=window.cwd, window_name=window.window_name
                    )
                    logger.info(
                        "Auto-resolved agy session for window %s: cid=%s, cwd=%s",
                        window_id,
                        cid,
                        window.cwd,
                    )
                return cid
        except Exception as e:
            logger.debug("Failed to auto-resolve agy window %s: %s", window_id, e)
        return None

    async def _record_session_map_entry(
        self, window_id: str, session_id: str, cwd: str = "", window_name: str = ""
    ) -> None:
        """Write session_map.json entry."""
        try:
            current: dict[str, Any] = {}
            if config.session_map_file.exists():
                async with aiofiles.open(config.session_map_file, "r") as f:
                    content = await f.read()
                    if content.strip():
                        current = json.loads(content)

            key = f"{config.tmux_session_name}:{window_id}"
            current[key] = {
                "session_id": session_id,
                "cwd": cwd,
                "window_name": window_name,
            }
            await asyncio.to_thread(atomic_write_json, config.session_map_file, current)
        except Exception as e:
            logger.warning("Error writing session_map: %s", e)

    async def resolve_session_for_window(self, window_id: str) -> AgySession | None:
        """Resolve the AgySession for a tmux window."""
        ws = self.get_window_state(window_id)
        if not ws.session_id:
            resolved_cid = await self.auto_resolve_agy_window(window_id)
            if resolved_cid:
                ws.session_id = resolved_cid

        if not ws.session_id:
            return None

        return await self.get_session_by_id(ws.session_id, ws.cwd)

    async def get_session_by_id(
        self, session_id: str, cwd: str = ""
    ) -> AgySession | None:
        """Get session information by conversation ID."""
        t_path = (
            config.agy_brain_path
            / session_id
            / ".system_generated"
            / "logs"
            / "transcript.jsonl"
        )
        if not t_path.exists():
            return None

        # Fetch title from SQLite DB if available
        title = ""
        step_count = 0
        if config.agy_db_path.exists():
            try:
                conn = sqlite3.connect(str(config.agy_db_path))
                row = conn.execute(
                    "SELECT title, step_count FROM conversation_summaries WHERE conversation_id = ?",
                    (session_id,),
                ).fetchone()
                conn.close()
                if row:
                    title = row[0] or ""
                    step_count = row[1] or 0
            except Exception:
                pass

        if not title:
            title = session_id[:8]

        return AgySession(
            session_id=session_id,
            summary=title,
            message_count=step_count,
            file_path=str(t_path),
            cwd=cwd,
        )

    async def list_recent_sessions_for_cwd(self, cwd: str) -> list[AgySession]:
        """List recent Antigravity sessions associated with a directory."""
        sessions: list[AgySession] = []
        if not config.agy_db_path.exists():
            return sessions

        try:
            norm_cwd = str(Path(cwd).resolve())
            conn = sqlite3.connect(str(config.agy_db_path))
            query = (
                "SELECT conversation_id, title, step_count "
                "FROM conversation_summaries "
                "WHERE workspace_uris LIKE ? "
                "ORDER BY last_modified_time DESC LIMIT 10"
            )
            rows = conn.execute(query, (f"%{norm_cwd}%",)).fetchall()
            conn.close()

            for cid, title, steps in rows:
                t_path = (
                    config.agy_brain_path
                    / cid
                    / ".system_generated"
                    / "logs"
                    / "transcript.jsonl"
                )
                if t_path.exists():
                    sessions.append(
                        AgySession(
                            session_id=cid,
                            summary=title or "Untitled",
                            message_count=steps or 0,
                            file_path=str(t_path),
                            cwd=norm_cwd,
                        )
                    )
        except Exception as e:
            logger.debug("Error querying agy db: %s", e)

        return sessions

    async def resolve_stale_ids(self) -> None:
        """Validate persisted window IDs against live tmux windows on startup."""
        windows = await tmux_manager.list_windows()
        live_wids = {w.window_id for w in windows}

        stale_threads = [
            tid for tid, wid in self.thread_bindings.items() if wid not in live_wids
        ]
        for tid in stale_threads:
            logger.warning(
                "Thread %d bound to non-existent window %s, removing",
                tid,
                self.thread_bindings[tid],
            )
            del self.thread_bindings[tid]

        # Auto-resolve live windows
        for w in windows:
            if w.window_name != config.tmux_main_window_name and w.window_id:
                await self.auto_resolve_agy_window(w.window_id)

        self._save_state()


session_manager = SessionManager()


def get_session_token_usage(transcript_path: Path | str) -> dict[str, int]:
    """Calculate token usage from a session's transcript.jsonl.

    Returns:
        dict with keys: 'input_tokens', 'output_tokens', 'cache_read_tokens', 'total_tokens'.
    """
    path = Path(transcript_path)
    totals = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "total_tokens": 0,
    }
    if not path.is_file():
        return totals

    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                if not line.strip() or ("token" not in line and "usage" not in line):
                    continue
                try:
                    data = json.loads(line)
                    in_tok = data.get("input_tokens") or 0
                    out_tok = data.get("output_tokens") or 0
                    cache_tok = data.get("cache_read_tokens") or 0
                    totals["input_tokens"] += in_tok
                    totals["output_tokens"] += out_tok
                    totals["cache_read_tokens"] += cache_tok
                except Exception:
                    continue
        totals["total_tokens"] = (
            totals["input_tokens"]
            + totals["output_tokens"]
            + totals["cache_read_tokens"]
        )
    except Exception as e:
        logger.debug("Error calculating token usage from %s: %s", path, e)

    return totals


def get_configured_model() -> tuple[str, str]:
    """Read configured model and agent mode from Antigravity settings.json."""
    settings_file = config.agy_data_dir / "settings.json"
    if settings_file.exists():
        try:
            d = json.loads(settings_file.read_text(encoding="utf-8"))
            return d.get("model", ""), d.get("agentMode", "")
        except Exception:
            pass
    return "", ""


async def get_live_session_model_info(window_id: str | None = None) -> dict[str, str]:
    """Retrieve model, mode, and effort info for the current session.

    Tries to parse live tmux pane status bar first (e.g. 'accept-edits · Gemini 3.8 Flash · high'),
    then falls back to ~/.gemini/antigravity-cli/settings.json.
    """
    info = {
        "model": "Gemini 3.8 Flash",
        "agent_mode": "accept-edits",
        "effort": "",
    }

    # 1. Try reading live status line from tmux pane if window_id provided
    if window_id:
        try:
            pane_text = await tmux_manager.capture_pane(
                window_id, start_line=-5, with_ansi=False
            )
            for line in reversed(pane_text.splitlines()):
                line = line.strip()
                # Status bar format: "mode · Model Name · effort" or similar
                m = re.search(
                    r"([a-zA-Z0-9_-]+)\s*·\s*([^·\n]+?)\s*·\s*([a-zA-Z0-9_-]+)", line
                )
                if m:
                    info["agent_mode"] = m.group(1).strip()
                    info["model"] = m.group(2).strip()
                    info["effort"] = m.group(3).strip()
                    return info
        except Exception as e:
            logger.debug(
                "Could not parse model from pane for window %s: %s", window_id, e
            )

    # 2. Fallback to settings.json
    cfg_model, cfg_mode = get_configured_model()
    if cfg_model:
        info["model"] = cfg_model
    if cfg_mode:
        info["agent_mode"] = cfg_mode

    return info
