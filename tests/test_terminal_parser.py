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


def test_extract_thought_from_terminal():
    from agybot.terminal_parser import extract_thought_from_terminal

    # Thought with duration and summary
    sample1 = "● Explored 1 file (migrate.js)\n○ Thought for 3m 55s (The goal is to integrate a global refresh event liste...)"
    th1 = extract_thought_from_terminal(sample1)
    assert th1 is not None
    assert "Thought for 3m 55s" in th1
    assert "global refresh event" in th1

    # Short thought with bullet
    sample2 = "▸ Thought for 7s\n● Ran 2 commands"
    th2 = extract_thought_from_terminal(sample2)
    assert th2 is not None
    assert th2 == "Thought for 7s"

    # In-progress thinking with summary
    sample3 = "○ Thinking... (analyzing project structure)"
    th3 = extract_thought_from_terminal(sample3)
    assert th3 is not None
    assert "analyzing project structure" in th3

    # Generic spinner without thought summary
    sample4 = "● Explored 1 file (test.py)\n● Ran 3 commands\n> "
    th4 = extract_thought_from_terminal(sample4)
    assert th4 is None
