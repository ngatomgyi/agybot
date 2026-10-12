"""Response message builder for Telegram delivery in agybot.

Prepares paginated response messages:
  - Formats content types (user input, assistant responses, thinking, tool calls)
  - Splits long messages into chunks within Telegram's 4096-character limit
"""

from __future__ import annotations

from ..telegram_sender import split_markdown_for_telegram
from ..transcript_parser import TranscriptParser


def build_response_parts(
    text: str,
    is_complete: bool,
    content_type: str = "text",
    role: str = "assistant",
) -> list[str]:
    """Build paginated response messages for Telegram.

    Returns a list of raw markdown strings, each fitting Telegram limits.
    """
    text = text.strip()
    if not text:
        return []

    # User message prefix
    if role == "user":
        if len(text) > 3000:
            text = text[:3000] + "…"
        return [f"👤 {text}"]

    # Truncate thinking if necessary
    if content_type == "thinking" and is_complete:
        start_tag = TranscriptParser.EXPANDABLE_QUOTE_START
        end_tag = TranscriptParser.EXPANDABLE_QUOTE_END
        max_thinking = 3500

        if start_tag in text and end_tag in text:
            prefix = text[: text.index(start_tag)]
            inner = text[text.index(start_tag) + len(start_tag) : text.index(end_tag)]
            if len(inner) > max_thinking:
                inner = inner[:max_thinking] + "\n\n… (thinking truncated)"
            text = f"{prefix}{start_tag}{inner}{end_tag}"
        elif len(text) > max_thinking:
            text = text[:max_thinking] + "\n\n… (thinking truncated)"

    chunks = split_markdown_for_telegram(text)
    if len(chunks) > 1:
        # Add pagination indicator
        return [
            f"{chunk}\n\n`[{i + 1}/{len(chunks)}]`" for i, chunk in enumerate(chunks)
        ]

    return chunks
