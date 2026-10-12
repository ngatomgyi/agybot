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


BUILD_EXTENSIONS: tuple[str, ...] = (".apk", ".aab", ".ipa")


def find_workspace_build_artifacts(
    cwd: str | Path,
    modified_after: float = 0.0,
) -> list[Path]:
    """Find built .apk, .aab, and .ipa binary files in a workspace directory.

    Scans common build output directories (Android Gradle, Flutter, React Native)
    as well as the workspace root.
    """
    root = Path(cwd).resolve()
    if not root.is_dir():
        return []

    # Priority search locations for Android / Mobile build outputs
    search_dirs = [
        root / "app" / "build" / "outputs" / "apk",
        root / "app" / "build" / "outputs" / "bundle",
        root / "build" / "app" / "outputs" / "flutter-apk",
        root / "build" / "app" / "outputs" / "bundle",
        root / "android" / "app" / "build" / "outputs",
        root / "build" / "outputs",
        root,
    ]

    seen: set[Path] = set()
    results: list[Path] = []
    ignored_parts = {
        ".git",
        ".venv",
        "node_modules",
        ".gradle",
        ".pytest_cache",
        ".ruff_cache",
    }

    for sdir in search_dirs:
        if not sdir.is_dir():
            continue
        try:
            for ext in BUILD_EXTENSIONS:
                pattern = f"*{ext}" if sdir == root else f"**/*{ext}"
                for cand in sdir.glob(pattern):
                    if cand in seen or not cand.is_file():
                        continue
                    if any(part in cand.parts for part in ignored_parts):
                        continue
                    try:
                        mtime = cand.stat().st_mtime
                        if mtime > modified_after:
                            seen.add(cand)
                            results.append(cand)
                    except OSError:
                        pass
        except Exception:
            pass

    return sorted(results, key=lambda p: p.stat().st_mtime, reverse=True)


def format_token_count(n: int) -> str:
    """Format token count with human-readable suffixes and exact comma-separated count.

    Examples:
        format_token_count(0) -> "0"
        format_token_count(540) -> "540"
        format_token_count(454741) -> "454.7k (454,741)"
        format_token_count(4337401) -> "4.34M (4,337,401)"
        format_token_count(104349687) -> "104.35M (104,349,687)"
    """
    if n <= 0:
        return "0"
    if n >= 1_000_000_000:
        return f"{n / 1_000_000_000:.2f}B ({n:,})"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f}M ({n:,})"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k ({n:,})"
    return f"{n:,}"
