"""Unit tests for agybot terminal_parser."""

from agybot.terminal_parser import (
    extract_interactive_content,
    is_interactive_ui,
    parse_status_line,
)


def test_is_interactive_ui():
    sample = """
    Select an option:
    [ ] 1. Option A
    [x] 2. Option B
    Enter to select
    """
    assert is_interactive_ui(sample)
    content = extract_interactive_content(sample)
    assert content is not None
    assert "Option A" in content.content


def test_parse_status_line():
    text = "Some output\n⠋ Running command: git status\n> "
    status = parse_status_line(text)
    assert status is not None
    assert "Running command" in status
