"""Tests for Telegram Developer API (Telethon) large file uploader and document dispatch."""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agybot.config import config
from agybot.handlers.message_sender import send_document
from agybot.telethon_uploader import (
    is_telethon_available,
    upload_large_file,
)


def test_is_telethon_available(monkeypatch):
    monkeypatch.setattr(config, "telegram_api_id", 123456)
    monkeypatch.setattr(config, "telegram_api_hash", "abcdef0123456789")
    assert is_telethon_available() is True
    assert config.has_telegram_developer_api is True

    monkeypatch.setattr(config, "telegram_api_id", None)
    monkeypatch.setattr(config, "telegram_api_hash", "")
    assert is_telethon_available() is False
    assert config.has_telegram_developer_api is False


@pytest.mark.asyncio
async def test_upload_large_file_via_developer_api(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(config, "telegram_api_id", 123456)
    monkeypatch.setattr(config, "telegram_api_hash", "abcdef0123456789")

    # Create dummy 55MB file
    large_file = tmp_path / "app-release.apk"
    large_file.write_bytes(b"dummy large apk content")

    mock_client = MagicMock()
    mock_client.is_connected = MagicMock(return_value=True)
    mock_client.send_file = AsyncMock()

    with patch(
        "agybot.telethon_uploader.get_telethon_client",
        new=AsyncMock(return_value=mock_client),
    ):
        success = await upload_large_file(
            chat_id=-1001234567890,
            file_input=large_file,
            filename="app-release.apk",
            caption="Release APK",
            message_thread_id=179121,
        )

        assert success is True
        mock_client.send_file.assert_called_once()
        call_kwargs = mock_client.send_file.call_args.kwargs
        assert call_kwargs["entity"] == -1001234567890
        assert call_kwargs["file"] == str(large_file)
        assert call_kwargs["caption"] == "Release APK"
        assert call_kwargs["reply_to"] == 179121
        assert call_kwargs["force_document"] is True
        assert len(call_kwargs["attributes"]) == 1
        assert call_kwargs["attributes"][0].file_name == "app-release.apk"


@pytest.mark.asyncio
async def test_send_document_routes_large_file_to_developer_api(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(config, "telegram_api_id", 123456)
    monkeypatch.setattr(config, "telegram_api_hash", "abcdef0123456789")

    large_file = tmp_path / "large_build.ipa"
    # Create file > 48MB
    large_file.write_bytes(b"0" * (50 * 1024 * 1024))

    mock_bot = MagicMock()
    mock_bot.send_document = AsyncMock()

    mock_upload = AsyncMock(return_value=True)
    monkeypatch.setattr("agybot.telethon_uploader.upload_large_file", mock_upload)

    await send_document(
        bot=mock_bot,
        chat_id=1227075616,
        document_data=[("large_build.ipa", large_file)],
        message_thread_id=555,
    )

    # Developer API should be used, standard bot.send_document bypassed
    mock_upload.assert_called_once_with(
        chat_id=1227075616,
        file_input=large_file,
        filename="large_build.ipa",
        caption=None,
        message_thread_id=555,
    )
    mock_bot.send_document.assert_not_called()


@pytest.mark.asyncio
async def test_send_document_uses_standard_api_for_small_file(tmp_path: Path):
    small_file = tmp_path / "small.txt"
    small_file.write_bytes(b"hello world")

    mock_bot = MagicMock()
    mock_bot.send_document = AsyncMock()

    mock_upload = AsyncMock()

    with patch("agybot.telethon_uploader.upload_large_file", mock_upload):
        await send_document(
            bot=mock_bot,
            chat_id=1227075616,
            document_data=[("small.txt", small_file)],
            message_thread_id=555,
        )

        mock_bot.send_document.assert_called_once()
        mock_upload.assert_not_called()


@pytest.mark.asyncio
async def test_send_document_fallback_to_developer_api_on_error(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(config, "telegram_api_id", 123456)
    monkeypatch.setattr(config, "telegram_api_hash", "abcdef0123456789")

    test_file = tmp_path / "borderline.apk"
    test_file.write_bytes(b"some content")

    mock_bot = MagicMock()
    mock_bot.send_document = AsyncMock(side_effect=Exception("File too large for standard bot API"))

    mock_upload = AsyncMock(return_value=True)
    monkeypatch.setattr("agybot.telethon_uploader.upload_large_file", mock_upload)

    await send_document(
        bot=mock_bot,
        chat_id=1227075616,
        document_data=[("borderline.apk", test_file)],
        message_thread_id=555,
    )

    # Standard bot tried first, failed, then Developer API succeeded
    mock_bot.send_document.assert_called_once()
    mock_upload.assert_called_once()
