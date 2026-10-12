"""Antigravity session transcript monitor.

Monitors transcript.jsonl logs in ~/.gemini/antigravity-cli/brain/<session_id>/,
tails new lines incrementally, parses them, detects generated artifacts,
and notifies the message handler.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Coroutine

import aiofiles

from .config import config
from .monitor_state import MonitorState
from .session import session_manager
from .transcript_parser import TranscriptParser
from .utils import BUILD_EXTENSIONS, find_workspace_build_artifacts

logger = logging.getLogger(__name__)


@dataclass
class NewMessage:
    """A new parsed message detected by the session monitor."""

    session_id: str
    text: str
    is_complete: bool
    content_type: str = "text"
    role: str = "assistant"
    tool_name: str | None = None
    image_data: list[tuple[str, bytes]] | None = None
    document_data: list[tuple[str, bytes]] | None = None


@dataclass
class SessionInfo:
    """Info about an active session file being watched."""

    session_id: str
    file_path: Path


class SessionMonitor:
    """Tails and parses Antigravity JSONL session transcripts."""

    def __init__(self) -> None:
        self.state = MonitorState(config.monitor_state_file)
        self.state.load()
        self._running = False
        self._message_callback: (
            Callable[[NewMessage], Coroutine[Any, Any, None]] | None
        ) = None
        # session_id -> set of artifact paths
        self._session_artifacts: dict[str, set[str]] = {}
        # session_id -> {artifact_path: last_mtime}
        self._sent_artifacts: dict[str, dict[str, float]] = {}

    def set_message_callback(
        self, callback: Callable[[NewMessage], Coroutine[Any, Any, None]]
    ) -> None:
        """Register the async callback for new messages."""
        self._message_callback = callback

    async def start(self) -> None:
        """Start the monitor polling loop."""
        if self._running:
            return
        self._running = True
        logger.info("Session monitor started")
        asyncio.create_task(self._monitor_loop())

    async def stop(self) -> None:
        """Stop the monitor."""
        self._running = False
        self.state.save()
        logger.info("Session monitor stopped")

    async def _monitor_loop(self) -> None:
        """Main polling loop."""
        while self._running:
            try:
                await self._poll_sessions()
            except Exception as e:
                logger.error("Error in session monitor loop: %s", e)
            await asyncio.sleep(config.monitor_poll_interval)

    async def _get_active_sessions(self) -> list[SessionInfo]:
        """Find active sessions from window states and session_map."""
        sessions: list[SessionInfo] = []
        seen_sids: set[str] = set()

        # Proactively ensure all bound windows have their session_id resolved
        self._periodic_check_counter = (
            getattr(self, "_periodic_check_counter", 0) + 1
        )
        periodic_sync = self._periodic_check_counter % 10 == 0

        for thread_id, wid in list(session_manager.thread_bindings.items()):
            ws = session_manager.get_window_state(wid)
            if not ws.session_id or periodic_sync:
                try:
                    await session_manager.auto_resolve_agy_window(wid)
                except Exception as e:
                    logger.debug(
                        "Failed to auto-resolve bound window %s in monitor: %s",
                        wid,
                        e,
                    )

        # 1. From live window states
        for ws in session_manager.window_states.values():
            if ws.session_id and ws.session_id not in seen_sids:
                t_path = (
                    config.agy_brain_path
                    / ws.session_id
                    / ".system_generated"
                    / "logs"
                    / "transcript.jsonl"
                )
                if t_path.exists():
                    sessions.append(
                        SessionInfo(session_id=ws.session_id, file_path=t_path)
                    )
                    seen_sids.add(ws.session_id)

        # 2. From session_map.json
        if config.session_map_file.exists():
            try:
                async with aiofiles.open(config.session_map_file, "r") as f:
                    content = await f.read()
                    if content.strip():
                        smap = json.loads(content)
                        for val in smap.values():
                            sid = val.get("session_id")
                            if sid and sid not in seen_sids:
                                t_path = (
                                    config.agy_brain_path
                                    / sid
                                    / ".system_generated"
                                    / "logs"
                                    / "transcript.jsonl"
                                )
                                if t_path.exists():
                                    sessions.append(
                                        SessionInfo(session_id=sid, file_path=t_path)
                                    )
                                    seen_sids.add(sid)
            except Exception as e:
                logger.debug("Error reading session_map in monitor: %s", e)

        return sessions

    async def _poll_sessions(self) -> None:
        """Poll all active sessions for new output."""
        sessions = await self._get_active_sessions()
        for session_info in sessions:
            try:
                await self._check_session(session_info)
            except Exception as e:
                logger.warning(
                    "Error checking session %s: %s", session_info.session_id, e
                )

        if self.state._dirty:
            self.state.save()

    async def _check_session(self, session_info: SessionInfo) -> None:
        """Check a single session file for new transcript entries."""
        file_path = session_info.file_path
        if not file_path.exists():
            return

        stat = await asyncio.to_thread(file_path.stat)
        current_size = stat.st_size
        last_offset = self.state.get_offset(session_info.session_id)

        # If file shrank, reset offset
        if current_size < last_offset:
            last_offset = 0

        if current_size == last_offset:
            return

        # Read new bytes
        new_lines: list[str] = []
        new_entries: list[dict[str, Any]] = []

        try:
            async with aiofiles.open(
                file_path, "r", encoding="utf-8", errors="replace"
            ) as f:
                await f.seek(last_offset)
                content = await f.read()
                new_offset = await f.tell()

                for line in content.splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    new_lines.append(line)
                    try:
                        new_entries.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
        except Exception as e:
            logger.warning("Error reading session file %s: %s", file_path, e)
            return

        self.state.update_offset(session_info.session_id, file_path, new_offset)

        if not new_entries:
            return

        # Track newly created artifacts from tool calls
        for data in new_entries:
            tool_calls = data.get("tool_calls", [])
            if isinstance(tool_calls, list):
                for tc in tool_calls:
                    if isinstance(tc, dict) and tc.get("name") in (
                        "write_to_file",
                        "replace_file_content",
                    ):
                        args = tc.get("args", {})
                        if isinstance(args, dict):
                            tf = str(args.get("TargetFile", "")).strip("\"' ")
                            meta = args.get("ArtifactMetadata")
                            if tf and (
                                meta
                                or (
                                    "/brain/" in tf
                                    and not any(
                                        x in tf
                                        for x in (
                                            "/scratch/",
                                            "/.system_generated/",
                                        )
                                    )
                                )
                            ):
                                self._session_artifacts.setdefault(
                                    session_info.session_id, set()
                                ).add(str(Path(tf).resolve()))

        # Also check brain directory for .md artifacts
        try:
            brain_dir = file_path.parents[2]
            if brain_dir.is_dir():
                for md_path in brain_dir.glob("*.md"):
                    if (
                        not md_path.name.endswith(".metadata.json")
                        and md_path.is_file()
                    ):
                        self._session_artifacts.setdefault(
                            session_info.session_id, set()
                        ).add(str(md_path.resolve()))
        except Exception as e:
            logger.debug(
                "Error checking brain_dir for %s: %s",
                session_info.session_id,
                e,
            )

        # Parse entries
        parsed_entries = TranscriptParser.parse_lines(new_lines)

        for entry in parsed_entries:
            if not entry.text and not entry.image_data and not entry.document_data:
                continue

            # Filtering rules
            if entry.role == "user" and not config.show_user_messages:
                continue
            if entry.content_type == "thinking" and not config.show_thinking:
                continue
            if (
                not config.show_tool_calls
                and entry.content_type in ("tool_use", "tool_result")
                and entry.tool_name != "ask_question"
            ):
                continue

            # Check for file:// links and explicit binary paths in text
            if entry.text:
                for m in re.finditer(
                    r"\[([^\]]+)\]\(file://([^\)#]+)(?:#[^\)]*)?\)", entry.text
                ):
                    linked = m.group(2).strip()
                    lp = Path(linked)
                    if lp.is_file() and (
                        lp.suffix.lower() == ".md"
                        or lp.suffix.lower() in BUILD_EXTENSIONS
                        or "/brain/" in linked
                    ):
                        self._session_artifacts.setdefault(
                            session_info.session_id, set()
                        ).add(str(lp.resolve()))

                for m in re.finditer(
                    r"(?:^|[\s\"'\(])(/[\w\.\-/]+?\.(?:apk|aab|ipa))(?=[\s\"'\)]|$)",
                    entry.text,
                ):
                    cand = Path(m.group(1).strip())
                    if cand.is_file():
                        self._session_artifacts.setdefault(
                            session_info.session_id, set()
                        ).add(str(cand.resolve()))

            # Attach artifacts to assistant text responses
            docs_to_send: list[tuple[str, bytes]] = []
            if entry.document_data:
                docs_to_send.extend(entry.document_data)

            final_text = entry.text
            if entry.role == "assistant" and entry.content_type == "text":
                # Auto-detect newly created build outputs in workspace
                cwd = session_manager.get_cwd_for_session(session_info.session_id)
                if cwd:
                    recent_builds = find_workspace_build_artifacts(cwd)
                    for bp in recent_builds:
                        self._session_artifacts.setdefault(
                            session_info.session_id, set()
                        ).add(str(bp.resolve()))

                candidates = list(
                    self._session_artifacts.pop(session_info.session_id, set())
                )
                seen_names: set[str] = {doc_name for doc_name, _ in docs_to_send}
                sent_map = self._sent_artifacts.setdefault(session_info.session_id, {})
                oversized_notices: list[str] = []

                for cand_path in candidates:
                    try:
                        cp = Path(cand_path)
                        if not cp.is_file():
                            continue
                        mtime = cp.stat().st_mtime
                        last_mtime = sent_map.get(cand_path, 0.0)
                        if mtime <= last_mtime:
                            continue

                        size = cp.stat().st_size
                        if size <= 48 * 1024 * 1024:
                            if cp.name not in seen_names:
                                docs_to_send.append((cp.name, cp.read_bytes()))
                                seen_names.add(cp.name)
                                sent_map[cand_path] = mtime
                        else:
                            sent_map[cand_path] = mtime
                            sz_mb = size / (1024 * 1024)
                            oversized_notices.append(
                                f"⚠️ Build output `{cp.name}` ({sz_mb:.1f} MB) exceeds Telegram's 50MB bot upload limit.\n📍 Saved on host at: `{cp}`"
                            )
                    except Exception as err:
                        logger.warning("Could not read artifact %s: %s", cand_path, err)

                if oversized_notices and final_text:
                    final_text = final_text + "\n\n" + "\n\n".join(oversized_notices)

            msg = NewMessage(
                session_id=session_info.session_id,
                text=final_text,
                is_complete=True,
                content_type=entry.content_type,
                role=entry.role,
                tool_name=entry.tool_name,
                image_data=entry.image_data,
                document_data=docs_to_send or None,
            )

            if self._message_callback:
                try:
                    await self._message_callback(msg)
                except Exception as e:
                    logger.error("Error executing message callback: %s", e)


session_monitor = SessionMonitor()
