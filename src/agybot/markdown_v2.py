"""Markdown → Telegram MarkdownV2 conversion layer.

Wraps `telegramify_markdown` renderer directly, disabling indented code blocks
and supporting table-to-card transformation and expandable blockquotes (>…||).
"""

from __future__ import annotations

import re

import mistletoe
from mistletoe.block_token import BlockCode, remove_token
from telegramify_markdown import _update_block, escape_latex
from telegramify_markdown.render import TelegramMarkdownRenderer

from .transcript_parser import TranscriptParser

_TABLE_SEP_RE = re.compile(r"^[\s|:\-]+$")


def _split_table_row(line: str) -> list[str]:
    """Split a table row by pipes, respecting escaped pipes (\\|)."""
    content = line.strip().strip("|")
    cells = re.split(r"(?<!\\)\|", content)
    return [cell.strip().replace("\\|", "|") for cell in cells]


def convert_markdown_tables(text: str) -> str:
    """Convert markdown tables to card-style key-value format for Telegram."""
    lines = text.split("\n")
    result: list[str] = []
    i = 0
    in_code_block = False

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        if stripped.startswith("```"):
            in_code_block = not in_code_block
            result.append(line)
            i += 1
            continue

        if in_code_block:
            result.append(line)
            i += 1
            continue

        # Look for table header + separator
        if (
            "|" in stripped
            and i + 1 < len(lines)
            and "|" in lines[i + 1]
            and _TABLE_SEP_RE.match(lines[i + 1].strip())
        ):
            headers = _split_table_row(line)
            i += 2
            table_rows: list[list[str]] = []

            while i < len(lines) and "|" in lines[i].strip():
                row_cells = _split_table_row(lines[i])
                table_rows.append(row_cells)
                i += 1

            for r_idx, row in enumerate(table_rows):
                if r_idx > 0:
                    result.append("─────")
                for c_idx, cell in enumerate(row):
                    header = (
                        headers[c_idx] if c_idx < len(headers) else f"Col {c_idx + 1}"
                    )
                    if cell:
                        result.append(f"**{header}**: {cell}")
            continue

        result.append(line)
        i += 1

    return "\n".join(result)


_EXPQUOTE_RE = re.compile(
    re.escape(TranscriptParser.EXPANDABLE_QUOTE_START)
    + r"([\s\S]*?)"
    + re.escape(TranscriptParser.EXPANDABLE_QUOTE_END)
)
_MDV2_ESCAPE_RE = re.compile(r"([_*\[\]()~`>#+\-=|{}.!\\])")


def _escape_mdv2(text: str) -> str:
    return _MDV2_ESCAPE_RE.sub(r"\\\1", text)


_EXPQUOTE_MAX_RENDERED = 3800


def _render_expandable_quote(m: re.Match[str]) -> str:
    inner = m.group(1)
    escaped = _escape_mdv2(inner)
    lines = escaped.split("\n")
    built: list[str] = []
    total_len = 0
    suffix = "\n>… \\(truncated\\)||"
    budget = _EXPQUOTE_MAX_RENDERED - len(suffix)
    truncated = False
    for line in lines:
        line_cost = 1 + len(line) + 1
        if total_len + line_cost > budget:
            remaining = budget - total_len - 2
            if remaining > 20:
                built.append(f">{line[:remaining]}")
            truncated = True
            break
        built.append(f">{line}")
        total_len += line_cost
    if truncated:
        return "\n".join(built) + suffix
    return "\n".join(built) + "||"


def _markdownify(text: str) -> str:
    """Render markdown using TelegramMarkdownRenderer."""
    with TelegramMarkdownRenderer(normalize_whitespace=False) as renderer:
        remove_token(BlockCode)
        content = escape_latex(text)
        document = mistletoe.Document(content)
        _update_block(document)
        return renderer.render(document)


def convert_markdown(text: str) -> str:
    """Convert standard markdown to Telegram MarkdownV2 format."""
    if not text:
        return ""

    text = convert_markdown_tables(text)

    segments: list[tuple[bool, str]] = []
    last_end = 0
    for m in _EXPQUOTE_RE.finditer(text):
        if m.start() > last_end:
            segments.append((False, text[last_end : m.start()]))
        segments.append((True, m.group(0)))
        last_end = m.end()
    if last_end < len(text):
        segments.append((False, text[last_end:]))

    if not segments:
        return _markdownify(text)

    parts: list[str] = []
    for is_quote, segment in segments:
        if is_quote:
            parts.append(_EXPQUOTE_RE.sub(_render_expandable_quote, segment))
        else:
            parts.append(_markdownify(segment))
    return "".join(parts)
