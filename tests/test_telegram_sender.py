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
