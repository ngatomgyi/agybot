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

    from agybot.handlers.message_queue import clear_thought_msg_info

    clear_thought_msg_info(1227075616, 178763)

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
    _status_msg_info[(1227075616, 178763)] = (100, "⚙️ *Running command…*\n`git status`")

    task = MessageTask(
        task_type="status",
        status_text="Running command…\ngit diff",
        thread_id=178763,
    )

    await _process_status_task(mock_bot, 1227075616, task)

    # Should edit message 100 in place
    mock_bot.edit_message_text.assert_called_once()
    assert (
        mock_bot.edit_message_text.call_args.kwargs["text"]
        == "⚙️ *Running command…*\n`git diff`"
    )
    assert mock_bot.send_message.call_count == 0
    assert _status_msg_info[(1227075616, 178763)][0] == 100


@pytest.mark.asyncio
async def test_process_status_task_replaces_spinner_with_dot():
    from agybot.handlers.message_queue import (
        _process_status_task,
        clear_status_msg_info,
    )
    from agybot.session import session_manager

    user_id = 1227075616
    thread_id = 987654
    session_manager.group_chat_ids = {f"{user_id}:{thread_id}": user_id}
    clear_status_msg_info(user_id, thread_id)

    mock_bot = MagicMock()
    mock_bot.send_message = AsyncMock(return_value=MagicMock(message_id=201))

    # Send status with raw Braille 3-dot spinner
    task = MessageTask(
        task_type="status",
        status_text="⠋ Working…",
        thread_id=thread_id,
    )
    await _process_status_task(mock_bot, user_id, task)

    mock_bot.send_message.assert_called_once()
    sent_text = mock_bot.send_message.call_args.kwargs["text"]
    assert sent_text == "⏳ *Working…*"

    # Test command emoji
    clear_status_msg_info(user_id, thread_id)
    task2 = MessageTask(
        task_type="status",
        status_text="Running command…\npytest",
        thread_id=thread_id,
    )
    await _process_status_task(mock_bot, user_id, task2)
    assert "⚙️ *Running command…*" in mock_bot.send_message.call_args.kwargs["text"]

    # Test read files emoji
    clear_status_msg_info(user_id, thread_id)
    task3 = MessageTask(
        task_type="status",
        status_text="Reading files…\nsrc/bot.py",
        thread_id=thread_id,
    )
    await _process_status_task(mock_bot, user_id, task3)
    assert "🔍 *Reading files…*" in mock_bot.send_message.call_args.kwargs["text"]

    # Test edit files emoji
    clear_status_msg_info(user_id, thread_id)
    task4 = MessageTask(
        task_type="status",
        status_text="Editing files…\nsrc/bot.py",
        thread_id=thread_id,
    )
    await _process_status_task(mock_bot, user_id, task4)
    assert "✏️ *Editing files…*" in mock_bot.send_message.call_args.kwargs["text"]

    # Test search web emoji
    clear_status_msg_info(user_id, thread_id)
    task5 = MessageTask(
        task_type="status",
        status_text="Searching web…\ntelegram api",
        thread_id=thread_id,
    )
    await _process_status_task(mock_bot, user_id, task5)
    assert "🌐 *Searching web…*" in mock_bot.send_message.call_args.kwargs["text"]


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
async def test_update_status_message_preserves_active_tool_with_spinner(monkeypatch):
    from agybot.handlers.message_queue import (
        set_active_tool,
        _user_queues,
    )
    from agybot.handlers.status_polling import update_status_message
    from agybot.tmux_manager import tmux_manager

    # Set active tool context
    set_active_tool(1227075616, 178763, "Running command…", "git diff")

    # Mock capture pane returning terminal spinner text
    monkeypatch.setattr(
        tmux_manager,
        "capture_pane",
        AsyncMock(return_value="Some output\n⣾  Running command...\n> "),
    )

    mock_bot = MagicMock()
    await update_status_message(mock_bot, 1227075616, "@1", 178763)

    # Verifies task was enqueued preserving command detail and updating spinner
    assert 1227075616 in _user_queues
    task = _user_queues[1227075616].get_nowait()
    assert task.task_type == "status"
    assert "git diff" in task.status_text
    assert "⚙️" in task.status_text


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


def test_drain_tasks_for_thread():
    from agybot.handlers.message_queue import (
        MessageTask,
        drain_tasks_for_thread,
        _user_queues,
    )
    import asyncio

    q = asyncio.Queue()
    _user_queues[999] = q

    q.put_nowait(MessageTask(task_type="status", thread_id=101, status_text="t1"))
    q.put_nowait(MessageTask(task_type="content", thread_id=102, parts=["other"]))
    q.put_nowait(MessageTask(task_type="status", thread_id=101, status_text="t2"))

    drain_tasks_for_thread(999, 101)

    assert not q.empty()
    remaining = q.get_nowait()
    assert remaining.thread_id == 102
    assert q.empty()


@pytest.mark.asyncio
async def test_update_status_message_idle_does_not_enqueue(monkeypatch):
    from agybot.handlers.message_queue import (
        clear_active_tool,
        _user_queues,
    )
    from agybot.handlers.status_polling import update_status_message
    from agybot.tmux_manager import tmux_manager

    # Explicitly clear active tool
    clear_active_tool(1227075616, 178763)

    monkeypatch.setattr(
        tmux_manager,
        "capture_pane",
        AsyncMock(return_value="⣾  Running command...\n> "),
    )

    mock_bot = MagicMock()
    # When idle (no ctx), update_status_message must do nothing
    await update_status_message(mock_bot, 1227075616, "@1", 178763)

    if 1227075616 in _user_queues:
        assert _user_queues[1227075616].empty()


def test_try_merge_content_tasks_separates_thinking_from_text():
    from agybot.handlers.message_queue import _try_merge_content_tasks

    q = asyncio.Queue()
    t_thinking = MessageTask(
        task_type="content",
        content_type="thinking",
        parts=["💭 *Thought:*\n>Plan||"],
        thread_id=10,
    )
    t_text = MessageTask(
        task_type="content",
        content_type="text",
        parts=["Final answer"],
        thread_id=10,
    )
    q.put_nowait(t_text)

    merged, merge_count = _try_merge_content_tasks(q, t_thinking)
    assert merge_count == 0
    assert merged.content_type == "thinking"
    assert merged.parts == ["💭 *Thought:*\n>Plan||"]
    assert not q.empty()
    assert q.get_nowait() == t_text


@pytest.mark.asyncio
async def test_process_content_task_thinking_preserves_active_tool_and_queue(
    monkeypatch,
):
    from agybot.handlers.inbound_queue import (
        clear_inbound_queue,
        get_queued_prompts,
        queue_inbound_prompt,
    )
    from agybot.handlers.message_queue import (
        get_active_tool,
        set_active_tool,
    )
    from agybot.session import session_manager

    user_id = 1227075616
    thread_id = 178763
    session_manager.group_chat_ids = {f"{user_id}:{thread_id}": user_id}

    mock_bot = MagicMock()
    mock_bot.send_message = AsyncMock()
    mock_bot.delete_message = AsyncMock()

    # Setup active tool and a queued inbound prompt
    set_active_tool(user_id, thread_id, "Running command…", "git status")
    clear_inbound_queue(user_id, thread_id)
    queue_inbound_prompt(user_id, thread_id, "next prompt in queue")
    _status_msg_info[(user_id, thread_id)] = (55, "⠋ Running command…")

    # 1. Processing thinking: deletes status msg, but MUST NOT clear active tool or dispatch prompt
    thinking_task = MessageTask(
        task_type="content",
        content_type="thinking",
        parts=["💭 *Thought:*\n>Thinking...||"],
        thread_id=thread_id,
    )
    await _process_content_task(mock_bot, user_id, thinking_task)

    # Status message was deleted so new status can appear below thought
    mock_bot.delete_message.assert_called_once_with(chat_id=user_id, message_id=55)
    # Active tool MUST STILL EXIST
    assert get_active_tool(user_id, thread_id) is not None
    # Queued prompt MUST STILL BE IN QUEUE
    assert len(get_queued_prompts(user_id, thread_id)) == 1

    # 2. Processing final text: deletes status msg, clears active tool, and dispatches queued prompt
    text_task = MessageTask(
        task_type="content",
        content_type="text",
        parts=["Done!"],
        thread_id=thread_id,
    )
    await _process_content_task(mock_bot, user_id, text_task)

    # Queued prompt was popped & dispatched, transitioning active tool to Working… for next prompt
    assert len(get_queued_prompts(user_id, thread_id)) == 0
    active = get_active_tool(user_id, thread_id)
    assert active is not None
    assert active.action == "Working…"


@pytest.mark.asyncio
async def test_thinking_actively_edits_single_message_in_place(monkeypatch):
    from agybot.handlers.message_queue import (
        _thought_msg_info,
        clear_thought_msg_info,
    )
    from agybot.session import session_manager
    from agybot.transcript_parser import TranscriptParser

    user_id = 1227075616
    thread_id = 999123
    session_manager.group_chat_ids = {f"{user_id}:{thread_id}": user_id}
    clear_thought_msg_info(user_id, thread_id)

    sent_msg = MagicMock()
    sent_msg.message_id = 777

    mock_bot = MagicMock()
    mock_bot.send_message = AsyncMock(return_value=sent_msg)
    mock_bot.edit_message_text = AsyncMock(return_value=sent_msg)
    mock_bot.delete_message = AsyncMock()

    # Step 1: First thinking task
    t1 = MessageTask(
        task_type="content",
        content_type="thinking",
        parts=[
            f"💭 *Thought:*\n{TranscriptParser.EXPANDABLE_QUOTE_START}Analyzing code...{TranscriptParser.EXPANDABLE_QUOTE_END}"
        ],
        thread_id=thread_id,
    )
    await _process_content_task(mock_bot, user_id, t1)

    # First thought sends a new message
    assert mock_bot.send_message.call_count == 1
    assert mock_bot.edit_message_text.call_count == 0
    assert (user_id, thread_id) in _thought_msg_info
    assert _thought_msg_info[(user_id, thread_id)].message_id == 777

    # Step 2: Second thinking task in same turn (after cooldown)
    _thought_msg_info[(user_id, thread_id)].last_edit_time = 0.0
    t2 = MessageTask(
        task_type="content",
        content_type="thinking",
        parts=[
            f"💭 *Thought:*\n{TranscriptParser.EXPANDABLE_QUOTE_START}Found the solution!{TranscriptParser.EXPANDABLE_QUOTE_END}"
        ],
        thread_id=thread_id,
    )
    await _process_content_task(mock_bot, user_id, t2)

    # Second thought MUST NOT send a new message — it must EDIT the existing message!
    assert mock_bot.send_message.call_count == 1
    assert mock_bot.edit_message_text.call_count == 1
    edit_kwargs = mock_bot.edit_message_text.call_args.kwargs
    assert edit_kwargs["message_id"] == 777
    assert "message_thread_id" not in edit_kwargs
    # Both thoughts are present in the edited message
    assert "Analyzing code" in edit_kwargs["text"]
    assert "Found the solution" in edit_kwargs["text"]

    # Step 3: Final turn response delivers content and deletes thought message
    text_task = MessageTask(
        task_type="content",
        content_type="text",
        parts=["All finished!"],
        thread_id=thread_id,
    )
    await _process_content_task(mock_bot, user_id, text_task)

    # Thought message 777 was deleted upon final response delivery
    mock_bot.delete_message.assert_called_with(chat_id=user_id, message_id=777)
    # Tracking cleared for next turn
    assert (user_id, thread_id) not in _thought_msg_info

    # Step 4: Next turn's thought creates a new message (total send_message = 1 (t1) + 1 (text) + 1 (t3) = 3)
    t3 = MessageTask(
        task_type="content",
        content_type="thinking",
        parts=[
            f"💭 *Thought:*\n{TranscriptParser.EXPANDABLE_QUOTE_START}Next turn thought{TranscriptParser.EXPANDABLE_QUOTE_END}"
        ],
        thread_id=thread_id,
    )
    await _process_content_task(mock_bot, user_id, t3)

    assert mock_bot.send_message.call_count == 3


@pytest.mark.asyncio
async def test_status_edit_cooldown_prevents_api_flood():
    from agybot.handlers.message_queue import (
        _process_status_task,
        _status_msg_info,
        _last_status_edit_time,
    )
    from agybot.session import session_manager

    user_id = 1227075616
    thread_id = 888123
    session_manager.group_chat_ids = {f"{user_id}:{thread_id}": user_id}

    _status_msg_info[(user_id, thread_id)] = (500, "⚙️ *Initial status*")
    _last_status_edit_time[(user_id, thread_id)] = asyncio.get_event_loop().time()

    mock_bot = MagicMock()
    mock_bot.edit_message_text = AsyncMock()

    # Immediate second status task within cooldown should be dropped/skipped
    task = MessageTask(
        task_type="status",
        status_text="⚙️ Fast changing status",
        thread_id=thread_id,
    )
    await _process_status_task(mock_bot, user_id, task)

    # edit_message_text should NOT be called because of cooldown
    assert mock_bot.edit_message_text.call_count == 0

    # After cooldown expires, edit should be dispatched
    _last_status_edit_time[(user_id, thread_id)] = 0.0
    await _process_status_task(mock_bot, user_id, task)
    assert mock_bot.edit_message_text.call_count == 1


@pytest.mark.asyncio
async def test_draining_lagging_thinking_tasks_on_final_text_delivery():
    from agybot.handlers.message_queue import (
        _process_content_task,
        clear_thought_msg_info,
        get_message_queue,
    )
    from agybot.session import session_manager

    user_id = 99887766
    thread_id = 777888
    session_manager.group_chat_ids = {f"{user_id}:{thread_id}": user_id}
    clear_thought_msg_info(user_id, thread_id)

    mock_bot = MagicMock()
    mock_bot.send_message = AsyncMock()
    mock_bot.delete_message = AsyncMock()

    queue = get_message_queue(user_id, mock_bot)

    # Put a lagging thinking task in the user queue
    lagging_thought = MessageTask(
        task_type="content",
        content_type="thinking",
        parts=["💭 Stale thought chunk that arrived late"],
        thread_id=thread_id,
    )
    queue.put_nowait(lagging_thought)
    assert queue.qsize() == 1

    # Now deliver final text task
    text_task = MessageTask(
        task_type="content",
        content_type="text",
        parts=["Final output for the user."],
        thread_id=thread_id,
    )
    await _process_content_task(mock_bot, user_id, text_task)

    # The lagging thinking task MUST be drained/discarded from queue!
    assert queue.qsize() == 0


def test_thought_persistence_roundtrip(tmp_path, monkeypatch):
    from agybot.handlers.message_queue import (
        ThoughtMessageInfo,
        _load_thought_msg_info,
        _save_thought_msg_info,
        _thought_msg_info,
    )
    from agybot.config import config

    monkeypatch.setattr(config, "config_dir", tmp_path)

    _thought_msg_info.clear()
    _thought_msg_info[(123, 456)] = ThoughtMessageInfo(
        message_id=999,
        thoughts=["Thinking part 1", "Thinking part 2"],
        last_edit_time=12345.67,
        chat_id=123,
    )
    _save_thought_msg_info()

    # Load in new dict
    loaded = _load_thought_msg_info()
    assert (123, 456) in loaded
    assert loaded[(123, 456)].message_id == 999
    assert loaded[(123, 456)].thoughts == ["Thinking part 1", "Thinking part 2"]
    assert loaded[(123, 456)].chat_id == 123


@pytest.mark.asyncio
async def test_auto_resolve_bound_windows_in_session_monitor(tmp_path, monkeypatch):
    from agybot.config import config
    from agybot.session import WindowState, session_manager
    from agybot.session_monitor import session_monitor

    brain_dir = tmp_path / "brain"
    brain_dir.mkdir(parents=True)
    monkeypatch.setattr(config, "agy_brain_path", brain_dir)

    cid = "test-auto-resolve-cid"
    t_file = brain_dir / cid / ".system_generated" / "logs" / "transcript.jsonl"
    t_file.parent.mkdir(parents=True)
    t_file.write_text('{"role": "assistant", "type": "PLANNER_RESPONSE", "content": "hi"}\n')

    wid = "@test_win"
    session_manager.thread_bindings = {9999: wid}
    session_manager.window_states[wid] = WindowState(window_id=wid, session_id="", cwd=str(tmp_path))

    async def mock_auto_resolve(target_wid):
        if target_wid == wid:
            session_manager.window_states[wid].session_id = cid
            return cid
        return None

    monkeypatch.setattr(session_manager, "auto_resolve_agy_window", mock_auto_resolve)

    active = await session_monitor._get_active_sessions()
    assert any(s.session_id == cid for s in active)
    assert session_manager.window_states[wid].session_id == cid


@pytest.mark.asyncio
async def test_clear_command_clears_window_session_id(monkeypatch):
    from agybot.bot import clear_command
    from agybot.session import WindowState, session_manager
    from agybot.tmux_manager import tmux_manager

    wid = "@clear_win"
    thread_id = 8888
    session_manager.thread_bindings = {thread_id: wid}
    session_manager.window_states[wid] = WindowState(window_id=wid, session_id="old-cid", cwd="/tmp")

    mock_update = MagicMock()
    mock_update.effective_user.id = 1227075616
    mock_update.effective_message.message_thread_id = thread_id
    mock_update.message.message_thread_id = thread_id
    mock_update.message.reply_text = AsyncMock()

    mock_context = MagicMock()

    monkeypatch.setattr(tmux_manager, "send_keys", AsyncMock())
    monkeypatch.setattr("agybot.bot.is_user_allowed", lambda uid: True)

    await clear_command(mock_update, mock_context)

    # Verifies session_id was cleared so next turn triggers re-resolution
    assert session_manager.get_window_state(wid).session_id == ""
    tmux_manager.send_keys.assert_called_once_with(wid, "/clear", enter=True)


