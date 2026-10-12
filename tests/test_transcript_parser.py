"""Unit tests for agybot TranscriptParser."""

from agybot.transcript_parser import TranscriptParser


def test_parse_user_input_with_tags():
    data = {
        "source": "USER_EXPLICIT",
        "type": "USER_INPUT",
        "content": "<USER_REQUEST>\nBuild an agybot service\n</USER_REQUEST>",
        "created_at": "2026-10-12T00:00:00Z",
    }
    entries = TranscriptParser.parse_entry(data)
    assert len(entries) == 1
    assert entries[0].role == "user"
    assert entries[0].text == "Build an agybot service"
    assert entries[0].content_type == "text"


def test_parse_model_thinking_and_content():
    data = {
        "source": "MODEL",
        "type": "PLANNER_RESPONSE",
        "thinking": "Planning the architecture",
        "content": "Here is the completed setup.",
        "created_at": "2026-10-12T00:00:01Z",
    }
    entries = TranscriptParser.parse_entry(data)
    assert len(entries) == 2
    assert entries[0].role == "assistant"
    assert entries[0].content_type == "thinking"
    assert "Planning the architecture" in entries[0].text

    assert entries[1].role == "assistant"
    assert entries[1].content_type == "text"
    assert entries[1].text == "Here is the completed setup."


def test_parse_model_tool_calls():
    data = {
        "source": "MODEL",
        "type": "PLANNER_RESPONSE",
        "tool_calls": [
            {
                "name": "run_command",
                "args": {"CommandLine": "ls -la /root/agybot"},
            },
            {
                "name": "write_to_file",
                "args": {"TargetFile": "/root/agybot/README.md"},
            },
        ],
    }
    entries = TranscriptParser.parse_entry(data)
    assert len(entries) == 2
    assert entries[0].content_type == "tool_use"
    assert entries[0].tool_name == "run_command"
    assert "ls -la /root/agybot" in entries[0].text

    assert entries[1].content_type == "tool_use"
    assert entries[1].tool_name == "write_to_file"
    assert "/root/agybot/README.md" in entries[1].text


def test_format_tool_summary_various_tools():
    summary_read = TranscriptParser.format_tool_summary(
        "view_file", {"AbsolutePath": "/root/agybot/README.md"}
    )
    assert summary_read == "**Reading files…**\n/root/agybot/README.md"

    summary_edit = TranscriptParser.format_tool_summary(
        "replace_file_content", {"TargetFile": "/root/agybot/src/main.py"}
    )
    assert summary_edit == "**Editing files…**\n/root/agybot/src/main.py"

    summary_subagent = TranscriptParser.format_tool_summary(
        "invoke_subagent",
        {"Subagents": [{"Role": "Code Reviewer", "TypeName": "reviewer"}]},
    )
    assert summary_subagent == "**Running subagent…**\nCode Reviewer"

    summary_web = TranscriptParser.format_tool_summary(
        "search_web", {"query": "python asyncio queue"}
    )
    assert summary_web == "**Searching web…**\npython asyncio queue"


def test_extract_history_messages():
    lines = [
        '{"source": "USER_EXPLICIT", "type": "USER_INPUT", "content": "<USER_REQUEST>Hi</USER_REQUEST>"}',
        '{"source": "MODEL", "type": "PLANNER_RESPONSE", "content": "Hello!"}',
    ]
    history = TranscriptParser.extract_history_messages(lines)
    assert len(history) == 2
    assert history[0].message_type == "user"
    assert history[0].text == "Hi"
    assert history[1].message_type == "assistant"
    assert history[1].text == "Hello!"
