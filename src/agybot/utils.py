"""Shared utility functions used across agybot modules.

Provides:
  - agybot_dir(): resolve config directory from AGYBOT_DIR env var or default ~/.agybot.
  - atomic_write_json(): crash-safe JSON file writes via temp+rename.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

AGYBOT_DIR_ENV = "AGYBOT_DIR"


def agybot_dir() -> Path:
    """Resolve config directory from AGYBOT_DIR env var or default ~/.agybot."""
    raw = os.environ.get(AGYBOT_DIR_ENV, "")
    return Path(raw) if raw else Path.home() / ".agybot"


def atomic_write_json(path: Path, data: Any, indent: int = 2) -> None:
    """Write JSON data to a file atomically.

    Writes to a temporary file in the same directory, then renames it
    to the target path. This prevents data corruption if the process
    is interrupted mid-write.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(data, indent=indent)

    fd, tmp_path = tempfile.mkstemp(
        dir=str(path.parent), suffix=".tmp", prefix=f".{path.name}."
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, str(path))
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
