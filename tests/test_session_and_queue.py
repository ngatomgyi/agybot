"""Tests for session manager resolution and message queue sending."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from agybot.handlers.message_queue import (
    MessageTask,
    _process_content_task,
    _process_status_task,
    _status_msg_info,
    _try_merge_status_tasks,
)
from agybot.session import SessionManager


def test_resolve_chat_id_with_user_id_zero(monkeypatch):
    sm = SessionManager()
    sm.group_chat_ids = {
        "1227075616:178763": 1227075616,
    }
    # When user_id is 0, it should resolve using thread_id mapping
    resolved = sm.resolve_chat_id(0, 178763)
    assert resolved == 1227075616

    # When user_id matches
    assert sm.resolve_chat_id(1227075616, 178763) == 1227075616

    # When thread_id not found but allowed_users configured
    from agybot.config import config

    monkeypatch.setattr(config, "allowed_users", [1227075616])
    assert sm.resolve_chat_id(0, 999999) == 1227075616


def test_resolve_user_id_for_thread():
    sm = SessionManager()
    sm.group_chat_ids = {
        "1227075616:178763": 1227075616,
    }
    assert sm.resolve_user_id_for_thread(178763) == 1227075616
    assert sm.resolve_user_id_for_thread(None) == 1227075616


@pytest.mark.asyncio
async def test_process_content_task_safely_calls_send(monkeypatch):
    from agybot.session import session_manager

    session_manager.group_chat_ids = {
        "1227075616:178763": 1227075616,
    }

    mock_bot = MagicMock()
    mock_bot.send_message = AsyncMock()
    mock_bot.delete_message = AsyncMock()

    # Pre-populate active status message
    _status_msg_info[(1227075616, 178763)] = (42, "⚙️ `run_command(git status)`")

    task = MessageTask(
        task_type="content",
        parts=["Hello Telegram"],
        thread_id=178763,
    )

    await _process_content_task(mock_bot, 1227075616, task)

    # Verifies ephemeral status message was deleted
    mock_bot.delete_message.assert_called_once_with(chat_id=1227075616, message_id=42)
    assert (1227075616, 178763) not in _status_msg_info

    # Verifies text message was sent
    mock_bot.send_message.assert_called_once()
    call_kwargs = mock_bot.send_message.call_args.kwargs
    assert call_kwargs["chat_id"] == 1227075616
    assert call_kwargs["message_thread_id"] == 178763
    assert "link_preview_options" in call_kwargs


@pytest.mark.asyncio
async def test_process_status_task_edits_existing_message(monkeypatch):
    from agybot.session import session_manager

    session_manager.group_chat_ids = {
        "1227075616:178763": 1227075616,
    }

    mock_bot = MagicMock()
    mock_bot.edit_message_text = AsyncMock()
    mock_bot.send_message = AsyncMock()

    # Existing status message
    _status_msg_info[(1227075616, 178763)] = (100, "⚙️ `run_command(git status)`")

    task = MessageTask(
        task_type="status",
        status_text="run_command(git diff)",
        thread_id=178763,
    )

    await _process_status_task(mock_bot, 1227075616, task)

    # Should edit message 100 in place
    mock_bot.edit_message_text.assert_called_once()
    assert mock_bot.send_message.call_count == 0
    assert _status_msg_info[(1227075616, 178763)][0] == 100


def test_try_merge_status_tasks():
    q = asyncio.Queue()
    t1 = MessageTask(task_type="status", status_text="run_command(1)", thread_id=10)
    t2 = MessageTask(task_type="status", status_text="run_command(2)", thread_id=10)
    t3 = MessageTask(task_type="status", status_text="run_command(3)", thread_id=10)
    t_other = MessageTask(task_type="content", parts=["done"], thread_id=10)

    q.put_nowait(t2)
    q.put_nowait(t3)
    q.put_nowait(t_other)

    latest, discard_count = _try_merge_status_tasks(q, t1)
    assert latest.status_text == "run_command(3)"
    assert discard_count == 2
    # The non-status task remains in the queue
    assert not q.empty()
    assert q.get_nowait() == t_other


@pytest.mark.asyncio
async def test_update_status_message_does_not_enqueue_generic_status(monkeypatch):
    from agybot.handlers.status_polling import update_status_message
    from agybot.tmux_manager import tmux_manager

    # Mock capture pane returning terminal spinner text
    monkeypatch.setattr(
        tmux_manager,
        "capture_pane",
        AsyncMock(return_value="Some output\n⣾  Running command...\n> "),
    )

    mock_bot = MagicMock()
    # update_status_message should NOT raise, and should NOT enqueue any generic status
    await update_status_message(mock_bot, 1227075616, "@1", 178763)
    # The bot queue should not have any tasks enqueued from update_status_message
    from agybot.handlers.message_queue import _user_queues

    if 1227075616 in _user_queues:
        assert _user_queues[1227075616].empty()


@pytest.mark.asyncio
async def test_cleanup_status_for_thread():
    from agybot.handlers.message_queue import (
        cleanup_status_for_thread,
        _status_msg_info,
        _save_status_msg_info,
        _load_status_msg_info,
    )
    from agybot.session import session_manager

    session_manager.group_chat_ids = {
        "1227075616:178763": 1227075616,
    }

    mock_bot = MagicMock()
    mock_bot.delete_message = AsyncMock()

    _status_msg_info[(1227075616, 178763)] = (200, "⚙️ `Running command… ls`")
    _save_status_msg_info()

    # Verify loaded from disk
    loaded = _load_status_msg_info()
    assert (1227075616, 178763) in loaded

    # Cleanup should delete message and remove from tracking
    await cleanup_status_for_thread(mock_bot, 1227075616, 178763)
    mock_bot.delete_message.assert_called_once_with(chat_id=1227075616, message_id=200)
    assert (1227075616, 178763) not in _status_msg_info

