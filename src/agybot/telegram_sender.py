"""Message splitting utility for Telegram's 4096-character limit.

Provides:
  - utf16_len(): message length in UTF-16 code units (Telegram accounting).
  - split_message(): splits long text into Telegram-safe chunks (≤4096 UTF-16 units),
    preferring newline boundaries and preserving markdown code block integrity.
"""

from __future__ import annotations

TELEGRAM_MAX_MESSAGE_LENGTH = 4096
TELEGRAM_SAFE_RAW_SPLIT_LENGTH = 2500
_FENCE_CLOSE_RESERVE = 4


def utf16_len(text: str) -> int:
    """Length in UTF-16 code units — how Telegram counts message length."""
    return sum(2 if ord(c) > 0xFFFF else 1 for c in text)


def _force_split(line: str, budget: int) -> list[str]:
    """Split a single overlong line into pieces within a UTF-16 budget."""
    pieces: list[str] = []
    piece = ""
    piece_len = 0
    for ch in line:
        ch_len = 2 if ord(ch) > 0xFFFF else 1
        if piece_len + ch_len > budget:
            pieces.append(piece)
            piece, piece_len = ch, ch_len
        else:
            piece += ch
            piece_len += ch_len
    if piece:
        pieces.append(piece)
    return pieces


def split_message(
    text: str, max_length: int = TELEGRAM_MAX_MESSAGE_LENGTH
) -> list[str]:
    """Split a message into chunks fitting Telegram's limit.

    Preserves code block integrity by automatically closing and reopening
    fenced blocks across chunks.
    """
    if utf16_len(text) <= max_length:
        return [text]

    chunks: list[str] = []
    current_chunk = ""
    current_len = 0
    in_code_block = False
    fence_lang = ""

    lines = text.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]
        line_len = utf16_len(line)

        # Budget calculation
        avail = max_length - current_len
        if in_code_block:
            avail -= _FENCE_CLOSE_RESERVE
        sep_len = 1 if current_chunk else 0

        # Handle overlong single line
        if not current_chunk and line_len > max_length:
            sublines = _force_split(line, max_length)
            chunks.extend(sublines[:-1])
            current_chunk = sublines[-1]
            current_len = utf16_len(current_chunk)
            i += 1
            continue

        # If line fits in current chunk
        if line_len + sep_len <= avail:
            if current_chunk:
                current_chunk += "\n" + line
                current_len += 1 + line_len
            else:
                current_chunk = line
                current_len = line_len

            # Check fence toggle
            stripped = line.strip()
            if stripped.startswith("```"):
                if in_code_block:
                    in_code_block = False
                    fence_lang = ""
                else:
                    in_code_block = True
                    fence_lang = stripped[3:].strip()
            i += 1
        else:
            # Chunk is full: flush it
            if in_code_block:
                current_chunk += "\n```"
            chunks.append(current_chunk)

            # Re-open in next chunk if inside code block
            if in_code_block:
                current_chunk = f"```{fence_lang}\n" if fence_lang else "```\n"
                current_len = utf16_len(current_chunk)
            else:
                current_chunk = ""
                current_len = 0

    if current_chunk:
        chunks.append(current_chunk)

    return chunks


def split_markdown_for_telegram(
    text: str,
    max_converted_len: int = 4000,
    safe_raw_len: int = TELEGRAM_SAFE_RAW_SPLIT_LENGTH,
) -> list[str]:
    """Split markdown text into chunks guaranteed to fit Telegram limits when converted to MarkdownV2.

    Accounts for character expansion caused by MarkdownV2 entity escaping.
    """
    from .markdown_v2 import convert_markdown

    if utf16_len(text) <= safe_raw_len:
        conv = convert_markdown(text)
        if utf16_len(conv) <= max_converted_len:
            return [text]

    chunks = split_message(text, max_length=safe_raw_len)
    final_chunks: list[str] = []
    for chunk in chunks:
        conv = convert_markdown(chunk)
        if utf16_len(conv) <= max_converted_len:
            final_chunks.append(chunk)
        else:
            sub_chunks = split_message(chunk, max_length=1500)
            for sub in sub_chunks:
                if utf16_len(convert_markdown(sub)) <= max_converted_len:
                    final_chunks.append(sub)
                else:
                    final_chunks.extend(split_message(sub, max_length=1000))
    return final_chunks
