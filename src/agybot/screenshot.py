"""Terminal text → PNG screenshot renderer with ANSI color support.

Converts captured tmux pane text (with ANSI color codes) into a
dark-background PNG image using a fallback font chain:
  1. JetBrains Mono
  2. Noto Sans Mono CJK SC
  3. Symbola
"""

from __future__ import annotations

import asyncio
import io
import logging
import re
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

logger = logging.getLogger(__name__)

_FONTS_DIR = Path(__file__).parent / "fonts"

_FONT_PATHS: list[Path] = [
    _FONTS_DIR / "JetBrainsMono-Regular.ttf",
    _FONTS_DIR / "NotoSansMonoCJKsc-Regular.otf",
    _FONTS_DIR / "Symbola.ttf",
]

# Basic 16 ANSI colors
_ANSI_COLORS: dict[int, tuple[int, int, int]] = {
    0: (0, 0, 0),
    1: (205, 49, 49),
    2: (13, 188, 121),
    3: (229, 229, 16),
    4: (36, 114, 200),
    5: (188, 63, 188),
    6: (17, 168, 205),
    7: (229, 229, 229),
    8: (102, 102, 102),
    9: (241, 76, 76),
    10: (35, 209, 139),
    11: (245, 245, 67),
    12: (59, 142, 234),
    13: (214, 112, 214),
    14: (41, 184, 219),
    15: (255, 255, 255),
}

_DEFAULT_FG = (204, 204, 204)
_DEFAULT_BG = (30, 30, 30)

_ANSI_RE = re.compile(r"\x1b\[([0-9;]*)m")


@dataclass
class TextSpan:
    text: str
    fg: tuple[int, int, int]
    bg: tuple[int, int, int] | None
    bold: bool = False


def _parse_ansi_spans(text: str) -> list[list[TextSpan]]:
    """Parse ANSI colored text into lines of TextSpans."""
    lines: list[list[TextSpan]] = []
    fg = _DEFAULT_FG
    bg: tuple[int, int, int] | None = None
    bold = False

    for raw_line in text.split("\n"):
        spans: list[TextSpan] = []
        pos = 0
        for match in _ANSI_RE.finditer(raw_line):
            chunk = raw_line[pos : match.start()]
            if chunk:
                spans.append(TextSpan(text=chunk, fg=fg, bg=bg, bold=bold))

            codes = [int(c) for c in match.group(1).split(";") if c.isdigit()]
            if not codes or 0 in codes:
                fg, bg, bold = _DEFAULT_FG, None, False
            else:
                idx = 0
                while idx < len(codes):
                    c = codes[idx]
                    if c == 1:
                        bold = True
                    elif c == 22:
                        bold = False
                    elif 30 <= c <= 37:
                        fg = _ANSI_COLORS[c - 30]
                    elif 90 <= c <= 97:
                        fg = _ANSI_COLORS[c - 90 + 8]
                    elif 40 <= c <= 47:
                        bg = _ANSI_COLORS[c - 40]
                    elif 100 <= c <= 107:
                        bg = _ANSI_COLORS[c - 100 + 8]
                    elif c in (38, 48) and idx + 2 < len(codes):
                        # 256 or RGB
                        mode = codes[idx + 1]
                        if mode == 5:
                            color_idx = codes[idx + 2]
                            rgb = (
                                _ANSI_COLORS.get(color_idx, _DEFAULT_FG)
                                if color_idx < 16
                                else (200, 200, 200)
                            )
                            if c == 38:
                                fg = rgb
                            else:
                                bg = rgb
                            idx += 2
                        elif mode == 2 and idx + 4 < len(codes):
                            rgb = (codes[idx + 2], codes[idx + 3], codes[idx + 4])
                            if c == 38:
                                fg = rgb
                            else:
                                bg = rgb
                            idx += 4
                    idx += 1
            pos = match.end()

        tail = raw_line[pos:]
        if tail:
            spans.append(TextSpan(text=tail, fg=fg, bg=bg, bold=bold))

        lines.append(spans)
    return lines


def _load_font(font_size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """Load the primary font with fallbacks."""
    for path in _FONT_PATHS:
        if path.exists():
            try:
                return ImageFont.truetype(str(path), font_size)
            except Exception:
                pass
    return ImageFont.load_default()


def render_terminal_image(
    text: str, font_size: int = 15, with_ansi: bool = True
) -> bytes:
    """Render terminal text to PNG image bytes."""
    if not with_ansi:
        text = _ANSI_RE.sub("", text)
        lines = [
            [TextSpan(text=line, fg=_DEFAULT_FG, bg=None)] for line in text.split("\n")
        ]
    else:
        lines = _parse_ansi_spans(text)

    # Drop trailing empty lines
    while lines and not any(s.text.strip() for s in lines[-1]):
        lines.pop()

    if not lines:
        lines = [[TextSpan(text=" (empty terminal) ", fg=_DEFAULT_FG, bg=None)]]

    font = _load_font(font_size)
    char_w = font_size * 0.6
    line_h = font_size * 1.35
    padding = 16

    max_cols = 0
    for row in lines:
        row_len = sum(len(s.text) for s in row)
        if row_len > max_cols:
            max_cols = row_len

    img_w = int(max(max_cols * char_w + padding * 2, 400))
    img_h = int(len(lines) * line_h + padding * 2)

    image = Image.new("RGB", (img_w, img_h), color=_DEFAULT_BG)
    draw = ImageDraw.Draw(image)

    y = padding
    for row in lines:
        x = padding
        for span in row:
            span_w = len(span.text) * char_w
            if span.bg:
                draw.rectangle(
                    [x, y, x + span_w, y + line_h],
                    fill=span.bg,
                )
            draw.text((x, y), span.text, font=font, fill=span.fg)
            x += span_w
        y += line_h

    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return buf.getvalue()


async def text_to_image_async(
    text: str, font_size: int = 15, with_ansi: bool = True
) -> bytes:
    """Async wrapper for render_terminal_image."""
    return await asyncio.to_thread(render_terminal_image, text, font_size, with_ansi)
