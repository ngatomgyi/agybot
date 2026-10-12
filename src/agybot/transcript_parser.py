"""Transcript parser specifically designed for Antigravity (agy) JSONL logs.

Extracts:
  - User messages (unwrapping <USER_REQUEST> tags)
  - Assistant responses (from PLANNER_RESPONSE steps)
  - Tool calls with clean parameter formatting
  - Model thinking / chain-of-thought blocks
  - Sentinels for expandable quotes
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class ParsedEntry:
    """A single parsed entry from the Antigravity transcript."""

    role: str  # "user", "assistant", "system"
    text: str
    content_type: str = "text"  # "text", "thinking", "tool_use", "tool_result"
    tool_name: str | None = None
    timestamp: str | None = None
    image_data: list[tuple[str, bytes]] | None = None
    document_data: list[tuple[str, bytes]] | None = None


@dataclass
class ParsedMessage:
    """A high-level message extracted from transcript history."""

    message_type: str  # "user", "assistant"
    text: str


class TranscriptParser:
    """Parser for Antigravity JSONL session transcripts."""

    EXPANDABLE_QUOTE_START = "\x02EXPQUOTE_START\x02"
    EXPANDABLE_QUOTE_END = "\x02EXPQUOTE_END\x02"
    _MAX_SUMMARY_LENGTH = 80

    @classmethod
    def format_tool_summary(cls, name: str, args: dict[str, Any]) -> str:
        """Format an Antigravity tool call summary for user display."""
        summary = ""
        if not isinstance(args, dict):
            args = {}

        if name == "run_command":
            cmd = args.get("CommandLine", "")
            if isinstance(cmd, str) and cmd.startswith('"') and cmd.endswith('"'):
                cmd = cmd[1:-1]
            summary = cmd
        elif name in ("view_file", "read_file"):
            path = args.get("AbsolutePath", "") or args.get("TargetFile", "")
            if isinstance(path, str) and path.startswith('"') and path.endswith('"'):
                path = path[1:-1]
            summary = path
        elif name in ("replace_file_content", "write_to_file", "edit_file"):
            path = args.get("TargetFile", "") or args.get("AbsolutePath", "")
            if isinstance(path, str) and path.startswith('"') and path.endswith('"'):
                path = path[1:-1]
            summary = path
        elif name == "ask_question":
            questions = args.get("questions", [])
            if isinstance(questions, list) and questions:
                first = questions[0]
                if isinstance(first, dict):
                    summary = first.get("question", "")
        elif name == "invoke_subagent":
            subagents = args.get("Subagents", [])
            if isinstance(subagents, list) and subagents:
                first = subagents[0]
                if isinstance(first, dict):
                    summary = first.get("Role", "") or first.get("TypeName", "")
        elif name == "read_url_content":
            summary = str(args.get("Url", "")).strip()
        elif name == "search_web":
            summary = str(args.get("query", "")).strip()
        else:
            for v in args.values():
                if isinstance(v, str) and v:
                    summary = v
                    break

        # Determine human-friendly action label
        action = f"**{name}…**"
        if name in ("run_command", "execute_command"):
            action = "**Running command…**"
        elif name in ("view_file", "read_file"):
            action = "**Reading files…**"
        elif name in ("replace_file_content", "edit_file"):
            action = "**Editing files…**"
        elif name == "write_to_file":
            action = "**Writing files…**"
        elif name == "search_web":
            action = "**Searching web…**"
        elif name == "read_url_content":
            action = "**Reading URL…**"
        elif name == "invoke_subagent":
            action = "**Running subagent…**"
        elif name == "ask_question":
            action = "**Asking question…**"

        if summary:
            if len(summary) > cls._MAX_SUMMARY_LENGTH:
                summary = summary[: cls._MAX_SUMMARY_LENGTH] + "…"
            return f"{action}\n{summary}"
        return action

    @classmethod
    def parse_entry(cls, data: dict[str, Any]) -> list[ParsedEntry]:
        """Parse a single JSON record from Antigravity's transcript.jsonl."""
        results: list[ParsedEntry] = []
        source = data.get("source")
        msg_type = data.get("type")
        timestamp = data.get("created_at") or data.get("timestamp")

        # 1. User Input
        if source == "USER_EXPLICIT" or msg_type == "USER_INPUT":
            content = data.get("content", "")
            if content:
                m = re.search(
                    r"<USER_REQUEST>\s*(.*?)\s*</USER_REQUEST>",
                    str(content),
                    re.DOTALL,
                )
                text = m.group(1).strip() if m else str(content).strip()
                if text:
                    results.append(
                        ParsedEntry(
                            role="user",
                            text=text,
                            content_type="text",
                            timestamp=timestamp,
                        )
                    )

        # 2. Model Execution (Thinking, Tool Calls, Content)
        elif source == "MODEL":
            # Thinking block
            thinking = data.get("thinking")
            if thinking:
                thinking_text = str(thinking).strip()
                if thinking_text:
                    results.append(
                        ParsedEntry(
                            role="assistant",
                            text=(
                                f"💭 *Thought:*\n"
                                f"{cls.EXPANDABLE_QUOTE_START}{thinking_text}{cls.EXPANDABLE_QUOTE_END}"
                            ),
                            content_type="thinking",
                            timestamp=timestamp,
                        )
                    )

            # Tool Calls
            tool_calls = data.get("tool_calls")
            if isinstance(tool_calls, list):
                for tc in tool_calls:
                    if isinstance(tc, dict):
                        t_name = tc.get("name", "")
                        t_args = tc.get("args", {})
                        summary = cls.format_tool_summary(t_name, t_args)
                        results.append(
                            ParsedEntry(
                                role="assistant",
                                text=summary,
                                content_type="tool_use",
                                tool_name=t_name,
                                timestamp=timestamp,
                            )
                        )

            # Assistant Text Response
            content = data.get("content")
            if content and msg_type == "PLANNER_RESPONSE":
                content_text = str(content).strip()
                if content_text:
                    results.append(
                        ParsedEntry(
                            role="assistant",
                            text=content_text,
                            content_type="text",
                            timestamp=timestamp,
                        )
                    )

        return results

    @classmethod
    def parse_lines(cls, lines: list[str]) -> list[ParsedEntry]:
        """Parse raw JSONL lines into parsed entries."""
        results: list[ParsedEntry] = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
                results.extend(cls.parse_entry(data))
            except json.JSONDecodeError:
                continue
        return results

    @classmethod
    def extract_history_messages(cls, lines: list[str]) -> list[ParsedMessage]:
        """Extract high-level user & assistant messages for conversation history."""
        messages: list[ParsedMessage] = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
                entries = cls.parse_entry(data)
                for entry in entries:
                    if entry.content_type == "text" and entry.text:
                        messages.append(
                            ParsedMessage(
                                message_type=entry.role,
                                text=entry.text,
                            )
                        )
            except json.JSONDecodeError:
                continue
        return messages
