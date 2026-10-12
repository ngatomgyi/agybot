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
