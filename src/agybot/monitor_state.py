"""Monitor state persistence — tracks byte offsets for each session.

Persists TrackedSession records to ~/.agybot/monitor_state.json so the
session monitor can resume incremental reading after restarts without
re-sending old messages.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .utils import atomic_write_json

logger = logging.getLogger(__name__)


@dataclass
class TrackedSession:
    """State for a tracked Antigravity session."""

    session_id: str
    file_path: str
    last_byte_offset: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TrackedSession:
        return cls(
            session_id=data.get("session_id", ""),
            file_path=data.get("file_path", ""),
            last_byte_offset=data.get("last_byte_offset", 0),
        )


@dataclass
class MonitorState:
    """Persistent state for the session monitor."""

    state_file: Path
    tracked_sessions: dict[str, TrackedSession] = field(default_factory=dict)
    _dirty: bool = field(default=False, repr=False)

    def load(self) -> None:
        """Load state from state_file."""
        if not self.state_file.exists():
            return
        try:
            data = json.loads(self.state_file.read_text(encoding="utf-8"))
            sessions_data = data.get("tracked_sessions", {})
            self.tracked_sessions = {
                sid: TrackedSession.from_dict(sdata)
                for sid, sdata in sessions_data.items()
            }
            logger.debug(
                "Loaded monitor state: %d sessions", len(self.tracked_sessions)
            )
        except Exception as e:
            logger.warning("Failed to load monitor state: %s", e)
            self.tracked_sessions = {}

    def save(self) -> None:
        """Save state to state_file atomically."""
        try:
            data = {
                "tracked_sessions": {
                    sid: s.to_dict() for sid, s in self.tracked_sessions.items()
                }
            }
            atomic_write_json(self.state_file, data)
            self._dirty = False
        except Exception as e:
            logger.warning("Failed to save monitor state: %s", e)

    def get_offset(self, session_id: str) -> int:
        """Get the last processed byte offset for a session."""
        session = self.tracked_sessions.get(session_id)
        return session.last_byte_offset if session else 0

    def update_offset(self, session_id: str, file_path: Path, offset: int) -> None:
        """Update the byte offset for a session."""
        existing = self.tracked_sessions.get(session_id)
        if existing:
            existing.last_byte_offset = offset
            existing.file_path = str(file_path)
        else:
            self.tracked_sessions[session_id] = TrackedSession(
                session_id=session_id,
                file_path=str(file_path),
                last_byte_offset=offset,
            )
        self._dirty = True

    def remove_session(self, session_id: str) -> None:
        """Remove a session from tracking."""
        if session_id in self.tracked_sessions:
            del self.tracked_sessions[session_id]
            self._dirty = True
            self.save()
