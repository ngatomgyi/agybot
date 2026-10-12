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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiofiles
import httpx

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


def render_quota_progress_bar(fraction: float, length: int = 15) -> str:
    """Render a progress bar [████████░░░░░░░]."""
    filled = max(0, min(length, int(round(fraction * length))))
    return "█" * filled + "░" * (length - filled)


def format_quota_refresh_time(reset_time_str: str) -> str:
    """Format an ISO timestamp into a user-friendly refresh delay."""
    if not reset_time_str:
        return ""
    try:
        dt = datetime.fromisoformat(reset_time_str.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        diff = dt - now
        secs = int(diff.total_seconds())
        if secs <= 0:
            return "Refreshes shortly"
        hours = secs // 3600
        mins = (secs % 3600) // 60
        days = hours // 24
        rem_hours = hours % 24
        if days > 0:
            return f"Refreshes in {hours}h {mins}m ({days}d {rem_hours}h)"
        return f"Refreshes in {hours}h {mins}m"
    except Exception:
        return ""


def parse_quota_from_pane(text: str) -> list[dict[str, Any]]:
    """Parse Models & Quota modal text captured from tmux pane."""
    groups: list[dict[str, Any]] = []
    group_chunks = re.split(r"\n(?=[A-Z\s]{4,}(?:MODELS)?\n)", text)
    for chunk in group_chunks:
        lines = [line.strip() for line in chunk.strip().splitlines() if line.strip()]
        if not lines:
            continue
        header = lines[0]
        if not ("MODELS" in header or "GEMINI" in header or "CLAUDE" in header):
            continue
        group_name = header
        desc = ""
        buckets = []
        i = 1
        while i < len(lines):
            cur_line = lines[i]
            if cur_line.startswith("Models within this group:"):
                desc = cur_line
                i += 1
                continue
            if "Limit Remaining" in cur_line:
                b_name = cur_line
                bar_line = lines[i + 1] if i + 1 < len(lines) else ""
                ref_line = lines[i + 2] if i + 2 < len(lines) else ""
                pct_match = re.search(r"(\d+(?:\.\d+)?)\s*%", bar_line)
                pct = pct_match.group(1) + "%" if pct_match else ""
                bar_match = re.search(r"\[([█░]+)\]", bar_line)
                bar = bar_match.group(1) if bar_match else ""
                buckets.append({
                    "name": b_name,
                    "percentage": pct,
                    "bar": bar,
                    "refresh": (
                        ref_line
                        if ("Refresh" in ref_line or "Quota" in ref_line)
                        else ""
                    ),
                })
                i += 3
                continue
            i += 1
        if buckets:
            groups.append({
                "group": group_name,
                "desc": desc,
                "buckets": buckets,
            })
    return groups


_cached_ls_address: str | None = None
_cached_csrf_token: str | None = None


async def _discover_antigravity_ls() -> tuple[str | None, str | None]:
    """Discover active Language Server Connect endpoint address and CSRF token."""
    global _cached_ls_address, _cached_csrf_token

    # 1. Test cached credentials first
    if _cached_ls_address and _cached_csrf_token:
        try:
            url = (
                f"http://{_cached_ls_address}/"
                "exa.language_server_pb.LanguageServerService/RetrieveUserQuotaSummary"
            )
            async with httpx.AsyncClient(timeout=1.5) as client:
                r = await client.post(
                    url,
                    headers={
                        "Content-Type": "application/json",
                        "x-codeium-csrf-token": _cached_csrf_token,
                    },
                    json={},
                )
                if r.status_code == 200:
                    return _cached_ls_address, _cached_csrf_token
        except Exception:
            _cached_ls_address = None
            _cached_csrf_token = None

    # 2. Check current process environment
    env_addr = os.environ.get("ANTIGRAVITY_LS_ADDRESS")
    env_csrf = os.environ.get("ANTIGRAVITY_CSRF_TOKEN")
    if env_addr and env_csrf:
        _cached_ls_address, _cached_csrf_token = env_addr, env_csrf
        return env_addr, env_csrf

    uuid_re = re.compile(
        rb"ANTIGRAVITY_CSRF_TOKEN="
        rb"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})"
    )
    ls_re = re.compile(rb"ANTIGRAVITY_LS_ADDRESS=(localhost:[0-9]+|127\.0\.0\.1:[0-9]+)")

    tokens: set[str] = set()
    ports: set[str] = set()

    # 3. Check /proc/*/environ
    for p in glob.glob("/proc/[0-9]*/environ"):
        try:
            with open(p, "rb") as ef:
                c = ef.read()
            for m in uuid_re.findall(c):
                tokens.add(m.decode())
            for m in ls_re.findall(c):
                ports.add(m.decode())
        except Exception:
            pass

    # 4. If tokens not found in environ, scan memory of running agy processes
    if not tokens:
        for cmd_path in glob.glob("/proc/[0-9]*/cmdline"):
            try:
                with open(cmd_path, "rb") as cf:
                    cmd = cf.read()
                if b"agy" not in cmd or b"agybot" in cmd:
                    continue
                pid = cmd_path.split("/")[2]
                with open(f"/proc/{pid}/maps", "r") as mf:
                    maps = mf.readlines()
                with open(f"/proc/{pid}/mem", "rb", 0) as mem:
                    for line in maps:
                        parts = line.split()
                        if "r" not in parts[1]:
                            continue
                        start, end = [int(x, 16) for x in parts[0].split("-")]
                        if end - start > 100 * 1024 * 1024:
                            continue
                        try:
                            mem.seek(start)
                            c = mem.read(end - start)
                            for m in uuid_re.findall(c):
                                tokens.add(m.decode())
                            for m in ls_re.findall(c):
                                ports.add(m.decode())
                        except Exception:
                            pass
            except Exception:
                pass

    token = next(iter(tokens)) if tokens else None
    port = next(iter(ports)) if ports else None

    if token and port:
        _cached_ls_address = port
        _cached_csrf_token = token
        return port, token

    return None, None


async def get_antigravity_quota_info(
    window_id: str | None = None,
) -> list[dict[str, Any]]:
    """Retrieve remaining usage quota limits and refresh countdowns.

    Tries Connect-RPC to the local Language Server first.
    If unavailable and window_id is provided, falls back to querying the live tmux pane.
    """
    # 1. Connect-RPC
    port, token = await _discover_antigravity_ls()
    if port and token:
        try:
            url = (
                f"http://{port}/"
                "exa.language_server_pb.LanguageServerService/RetrieveUserQuotaSummary"
            )
            async with httpx.AsyncClient(timeout=2.5) as client:
                r = await client.post(
                    url,
                    headers={
                        "Content-Type": "application/json",
                        "x-codeium-csrf-token": token,
                    },
                    json={},
                )
                if r.status_code == 200:
                    data = r.json()
                    groups = data.get("response", {}).get("groups", [])
                    result = []
                    for g in groups:
                        g_name = g.get("displayName", "")
                        g_desc = g.get("description", "")
                        buckets = []
                        for b in g.get("buckets", []):
                            frac = float(b.get("remainingFraction", 0.0))
                            pct = round(frac * 100, 1)
                            reset_time = b.get("resetTime", "")
                            refresh_text = (
                                "Quota available"
                                if frac >= 0.999
                                else format_quota_refresh_time(reset_time)
                            )
                            buckets.append({
                                "name": b.get("displayName", "Limit Remaining"),
                                "fraction": frac,
                                "percentage": f"{pct}%",
                                "bar": render_quota_progress_bar(frac, length=15),
                                "reset_time": reset_time,
                                "refresh": refresh_text,
                                "description": b.get("description", ""),
                            })
                        if buckets:
                            result.append({
                                "group": g_name,
                                "desc": g_desc,
                                "buckets": buckets,
                            })
                    if result:
                        return result
        except Exception as e:
            logger.debug("RetrieveUserQuotaSummary RPC error: %s", e)

    # 2. Tmux pane modal fallback
    if window_id:
        try:
            pane_tail = await tmux_manager.capture_pane(
                window_id, start_line=-8, with_ansi=False
            )
            if (
                "Running command" not in pane_tail
                and "Thinking..." not in pane_tail
                and "Working…" not in pane_tail
            ):
                await tmux_manager.send_keys(window_id, "/usage", enter=True)
                await asyncio.sleep(0.4)
                modal_pane = await tmux_manager.capture_pane(
                    window_id, start_line=-35, with_ansi=False
                )
                await tmux_manager.send_keys(window_id, "Escape", enter=False)
                parsed = parse_quota_from_pane(modal_pane)
                if parsed:
                    return parsed
        except Exception as e:
            logger.debug("Tmux modal quota parse error for %s: %s", window_id, e)

    return []


def format_quota_markdown(quota_groups: list[dict[str, Any]]) -> str:
    """Format quota limits and countdowns into readable markdown."""
    if not quota_groups:
        return ""

    lines = ["📈 *Usage Limits & Quota Remaining*"]
    for g in quota_groups:
        name = g.get("group", "").strip()
        desc = g.get("desc", "")
        models_sub = re.sub(
            r"^Models within this group:\s*", "", desc, flags=re.IGNORECASE
        ).strip()
        if models_sub:
            lines.append(f"\n*{name}* ({models_sub})")
        else:
            lines.append(f"\n*{name}*")

        for b in g.get("buckets", []):
            b_name = b.get("name", "Limit")
            bar = b.get("bar", "")
            pct = b.get("percentage", "")
            refresh = b.get("refresh", "")
            b_label = b_name.replace(" Remaining", "")

            lines.append(f"• *{b_label}:* `[{bar}]` *{pct}*")
            if refresh and refresh != "Quota available":
                lines.append(f"  ⏳ _{refresh}_")
            elif refresh == "Quota available":
                lines.append("  ✨ _Quota available_")

    return "\n".join(lines)

