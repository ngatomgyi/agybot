"""Unit tests for agybot telegram_sender."""

from agybot.telegram_sender import split_message, utf16_len


def test_utf16_len():
    assert utf16_len("hello") == 5
    assert utf16_len("🚀") == 2
    assert utf16_len("🤖 robot") == 8


def test_split_short_message():
    text = "Short message that easily fits"
    chunks = split_message(text, max_length=100)
    assert chunks == [text]


def test_split_message_preserves_code_blocks():
    text = (
        "Intro text\n```python\nfor i in range(10):\n    print(i)\n```\nConclusion text"
    )
    chunks = split_message(text, max_length=35)
    assert len(chunks) > 1
    # First chunk containing code block should close with ```
    assert chunks[0].endswith("```")
    # Second chunk should reopen with ```
    assert chunks[1].startswith("```")


def test_split_markdown_for_telegram_expansion():
    from agybot.markdown_v2 import convert_markdown
    from agybot.telegram_sender import split_markdown_for_telegram

    # Create text with heavy formatting (tables, bullet points, numbers) that expands significantly
    lines = ["| Col A | Col B | Col C |", "|---|---|---|"]
    for i in range(70):
        lines.append(f"| Item {i}.0 | Details {i}-sub (info) | Value {i}! |")
    text = "\n".join(lines)

    # Raw length is around 3500 chars, but converted MarkdownV2 would exceed 4500 chars
    conv_raw = convert_markdown(text)
    assert utf16_len(conv_raw) > 4000

    chunks = split_markdown_for_telegram(text, max_converted_len=4000)
    assert len(chunks) >= 2
    for chunk in chunks:
        conv_chunk = convert_markdown(chunk)
        assert utf16_len(conv_chunk) <= 4000
