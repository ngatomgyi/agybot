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
