"""Tests for build artifacts detection and inbound attachment handling."""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from agybot.utils import find_workspace_build_artifacts


def test_find_workspace_build_artifacts(tmp_path: Path):
    # Setup simulated project directory structure
    apk_dir = tmp_path / "app" / "build" / "outputs" / "apk" / "debug"
    apk_dir.mkdir(parents=True)
    apk_file = apk_dir / "app-debug.apk"
    apk_file.write_bytes(b"dummy apk content")

    bundle_dir = tmp_path / "app" / "build" / "outputs" / "bundle" / "release"
    bundle_dir.mkdir(parents=True)
    aab_file = bundle_dir / "app-release.aab"
    aab_file.write_bytes(b"dummy aab content")

    # Ignored directory
    venv_dir = tmp_path / ".venv" / "outputs"
    venv_dir.mkdir(parents=True)
    (venv_dir / "ignored.apk").write_bytes(b"ignored")

    found = find_workspace_build_artifacts(tmp_path)
    found_names = [p.name for p in found]

    assert "app-debug.apk" in found_names
    assert "app-release.aab" in found_names
    assert "ignored.apk" not in found_names


@pytest.mark.asyncio
async def test_handle_inbound_photo_attachment(monkeypatch, tmp_path: Path):
    from agybot.bot import handle_inbound_message
    from agybot.session import session_manager
    from agybot.tmux_manager import tmux_manager

    # Setup mock window and session
    wid = "@1"
    thread_id = 999
    session_manager.thread_bindings[thread_id] = wid
    session_manager.window_states[wid] = MagicMock(cwd=str(tmp_path))

    # Mock tmux send_keys
    mock_send_keys = AsyncMock(return_value=True)
    monkeypatch.setattr(tmux_manager, "send_keys", mock_send_keys)

    # Mock Telegram Update and Bot
    mock_user = MagicMock(id=1227075616)
    mock_photo = MagicMock(file_id="photo_123")
    mock_message = MagicMock(
        message_id=42,
        chat_id=1227075616,
        photo=[mock_photo],
        document=None,
        caption="Fix this button UI",
        text=None,
        message_thread_id=thread_id,
    )
    mock_update = MagicMock(
        effective_user=mock_user,
        message=mock_message,
        effective_message=mock_message,
    )

    # Mock bot.get_file and file.download_to_drive
    mock_file = MagicMock()
    mock_file.download_to_drive = AsyncMock()
    mock_bot = MagicMock()
    mock_bot.get_file = AsyncMock(return_value=mock_file)
    mock_context = MagicMock(bot=mock_bot)

    await handle_inbound_message(mock_update, mock_context)

    # Assert download_to_drive was called
    mock_file.download_to_drive.assert_called_once()
    saved_path = mock_file.download_to_drive.call_args[0][0]
    assert "attachments" in str(saved_path)
    assert saved_path.name.endswith(".jpg")

    # Assert prompt was constructed and sent to tmux
    mock_send_keys.assert_called_once()
    called_wid, called_prompt = mock_send_keys.call_args[0][:2]
    assert called_wid == wid
    assert "[Attached image:" in called_prompt
    assert "Fix this button UI" in called_prompt


def test_build_screenshot_keyboard_has_close_button():
    from agybot.bot import _build_screenshot_keyboard
    from agybot.handlers.callback_data import (
        CB_SCREENSHOT_CLOSE,
        CB_SCREENSHOT_REFRESH,
    )

    kb = _build_screenshot_keyboard("@42")
    # Last row should have y, n, Refresh, Close
    last_row = kb.inline_keyboard[-1]
    button_texts = [btn.text for btn in last_row]
    callback_data = [btn.callback_data for btn in last_row]

    assert "🔄 Refresh" in button_texts
    assert "❌ Close" in button_texts
    assert f"{CB_SCREENSHOT_REFRESH}@42" in callback_data
    assert f"{CB_SCREENSHOT_CLOSE}@42" in callback_data
