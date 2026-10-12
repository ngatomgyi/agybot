"""Terminal output parser — detects Antigravity UI elements in tmux pane text.

Parses captured tmux pane content to detect:
  - Interactive prompts (ask_question, permission prompts, confirmations)
  - Active status lines (Thought for ..., Running command ..., spinner states)
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass
class InteractiveUIContent:
    """Content extracted from an interactive UI."""

    content: str
    name: str = ""


@dataclass(frozen=True)
class UIPattern:
    """A text-marker pair that delimits an interactive UI region."""

    name: str
    top: tuple[re.Pattern[str], ...]
    bottom: tuple[re.Pattern[str], ...]
    min_gap: int = 1


# UI patterns for detecting interactive prompts in Antigravity TUI
UI_PATTERNS: list[UIPattern] = [
    # Multi-tab or choice selection
    UIPattern(
        name="AskQuestion",
        top=(
            re.compile(r"^\s*←\s+[☐✔☒]"),
            re.compile(r"^\s*[☐✔☒]"),
            re.compile(r"^\s*\[[ xX]\]"),
        ),
        bottom=(
            re.compile(r"^\s*Enter to (select|confirm|submit)"),
            re.compile(r"^\s*Esc to cancel"),
        ),
        min_gap=1,
    ),
    # Confirmation prompts
    UIPattern(
        name="ConfirmationPrompt",
        top=(
            re.compile(r"^\s*Do you want to proceed\?"),
            re.compile(r"^\s*Do you want to make this edit"),
            re.compile(r"^\s*This command requires approval"),
            re.compile(r"^\s*Would you like to proceed\?"),
        ),
        bottom=(
            re.compile(r"^\s*Esc to cancel"),
            re.compile(r"^\s*Enter to confirm"),
        ),
        min_gap=1,
    ),
    # Interactive menu with arrows
    UIPattern(
        name="MenuPrompt",
        top=(
            re.compile(r"^\s*❯\s*1\.\s*"),
            re.compile(r"^\s*❯\s+"),
        ),
        bottom=(
            re.compile(r"^\s*Esc to (cancel|exit)"),
            re.compile(r"^\s*Enter to select"),
        ),
        min_gap=1,
    ),
]

_RE_LONG_DASH = re.compile(r"^─{5,}$")


def _shorten_separators(text: str) -> str:
    """Replace long horizontal lines with clean 5-dash lines."""
    return "\n".join(
        "─────" if _RE_LONG_DASH.match(line) else line for line in text.split("\n")
    )


def extract_interactive_content(text: str) -> InteractiveUIContent | None:
    """Extract interactive UI content from raw terminal output."""
    lines = text.split("\n")
    for pat in UI_PATTERNS:
        start_idx: int | None = None
        for i, line in enumerate(lines):
            if any(t.search(line) for t in pat.top):
                start_idx = i
                break

        if start_idx is None:
            continue

        end_idx: int | None = None
        if not pat.bottom:
            end_idx = len(lines) - 1
        else:
            for j in range(start_idx + pat.min_gap, len(lines)):
                if any(b.search(lines[j]) for b in pat.bottom):
                    end_idx = j
                    break

        if end_idx is not None:
            extracted = "\n".join(lines[start_idx : end_idx + 1]).strip()
            if extracted:
                return InteractiveUIContent(
                    content=_shorten_separators(extracted),
                    name=pat.name,
                )

    return None


def is_interactive_ui(text: str) -> bool:
    """Check if the pane text currently displays an interactive prompt."""
    return extract_interactive_content(text) is not None


_SPINNERS = {
    "⠋",
    "⠙",
    "⠹",
    "⠸",
    "⠼",
    "⠴",
    "⠦",
    "⠧",
    "⠇",
    "⠏",
    "⡿",
    "⣟",
    "⣯",
    "⣷",
    "⣾",
    "⣽",
    "⣻",
    "⢿",
    "○",
    "●",
    "▸",
}


def is_terminal_idle_at_prompt(lines: list[str]) -> bool:
    """Check if the terminal is currently at the idle prompt waiting for user input."""
    bottom_lines = [line.strip() for line in lines if line.strip()][-6:]
    if not bottom_lines:
        return False
    # If esc to cancel or ctrl+c is present, agent is actively executing
    has_cancel = any(
        "esc to cancel" in line.lower() or "ctrl+c" in line.lower()
        for line in bottom_lines
    )
    if has_cancel:
        return False

    # Check if last non-empty line ends at prompt `>` or prompt + shortcuts
    last = bottom_lines[-1]
    if last == ">" or last.startswith("> "):
        return True
    if len(bottom_lines) >= 2 and any(
        line == ">" or line.startswith("> ") for line in bottom_lines[-3:]
    ):
        if any("? for shortcuts" in line or "shortcuts" in line for line in bottom_lines[-2:]):
            return True

    return False


def extract_thought_from_terminal(text: str) -> str | None:
    """Extract thought summary from terminal pane if present."""
    lines = [line.strip() for line in text.split("\n") if line.strip()]
    if not lines:
        return None

    prefixes = list(_SPINNERS) + ["•", "*", "-", "🧠", "💭"]
    for line in reversed(lines[-15:]):
        clean = line
        for prefix in prefixes:
            if clean.startswith(prefix):
                clean = clean[len(prefix) :].strip()
                break

        clean_lower = clean.lower()
        if clean_lower.startswith("thought for "):
            return clean
        if clean_lower.startswith("thinking...") or clean_lower.startswith("thinking…"):
            if len(clean) > len("thinking..."):
                return clean
    return None


def parse_status_line(text: str) -> str | None:
    """Parse the active agent status line (spinner, thinking, tool running)."""
    lines = [line.strip() for line in text.split("\n") if line.strip()]
    if not lines:
        return None

    for line in reversed(lines[-6:]):
        # Match spinner or thinking patterns
        if any(line.startswith(sp) for sp in _SPINNERS):
            return line
        if "Thought for " in line or "Thinking..." in line:
            return line
        if "Running command" in line or "Explored " in line:
            return line

    return None


def extract_spinner_and_status(text: str) -> tuple[str, str] | None:
    """Extract (spinner_char, status_text) from active terminal output."""
    lines = [line.strip() for line in text.split("\n") if line.strip()]
    if not lines:
        return None

    for line in reversed(lines[-6:]):
        for sp in _SPINNERS:
            if line.startswith(sp):
                remainder = line[len(sp) :].strip()
                return (sp, remainder or "Working…")
        if "Thought for " in line:
            return ("🧠", line)
        if "Thinking..." in line or "Thinking…" in line:
            return ("💭", "Thinking…")
        if "Running command" in line:
            return ("⚡", "Running command…")
        if "Explored " in line:
            return ("🔍", line)

    return None


def strip_pane_chrome(text: str) -> str:
    """Strip input bar and bottom status chrome from pane capture."""
    lines = text.split("\n")
    # Remove bottom trailing prompt lines
    while lines and (
        not lines[-1].strip()
        or lines[-1].strip().startswith(">")
        or "esc to cancel" in lines[-1].lower()
    ):
        lines.pop()
    return "\n".join(lines).rstrip()
