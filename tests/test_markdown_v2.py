"""Unit tests for agybot markdown_v2."""

from agybot.markdown_v2 import convert_markdown, convert_markdown_tables


def test_convert_markdown_tables():
    md_table = """
| Parameter | Value |
| --- | --- |
| Model | Gemini 3.8 Flash |
| Mode | accept-edits |
"""
    result = convert_markdown_tables(md_table)
    assert "**Parameter**: Model" in result
    assert "**Value**: Gemini 3.8 Flash" in result
    assert "**Parameter**: Mode" in result
    assert "**Value**: accept-edits" in result


def test_convert_markdown_basic():
    text = "Hello **world**"
    converted = convert_markdown(text)
    assert "world" in converted


def test_convert_markdown_expandable_quote():
    from agybot.transcript_parser import TranscriptParser

    text = (
        f"💭 *Thought:*\n"
        f"{TranscriptParser.EXPANDABLE_QUOTE_START}"
        f"Thinking deeply about life\nLine 2"
        f"{TranscriptParser.EXPANDABLE_QUOTE_END}"
    )
    converted = convert_markdown(text)
    assert ">Thinking deeply about life" in converted
    assert ">Line 2||" in converted
    assert "Thought" in converted


def test_convert_markdown_file_links():
    text = "Inspect [`/root/test.py`](file:///root/test.py) and [README.md](file:///README.md)"
    converted = convert_markdown(text)
    assert "file://" not in converted
    assert "test\\.py" in converted or "test.py" in converted
    assert "README\\.md" in converted or "README.md" in converted


def test_convert_markdown_sanitizes_blockquotes():
    text = "- Item description:\n  > *\"Quoted advice\"*\n  > Second line"
    converted = convert_markdown(text)
    # The blockquote should not have unescaped > preceded by non-newline characters
    import re

    assert not re.search(r"(?<!^)(?<!\n)>", converted)


def test_convert_markdown_file_links_with_parenthesis_in_path():
    text = "Bug in [reports page](file:///root/pa/src/app/(app)/reports/page.tsx#L675-L685)."
    converted = convert_markdown(text)
    assert "file://" not in converted
    assert "page\\.tsx" in converted or "page.tsx" in converted
    assert "reports page" in converted
    # Verify no raw unclosed parenthesis suffix like '/reports/page.tsx#L675-L685)'
    assert "/reports/page.tsx" not in converted
