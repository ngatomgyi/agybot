"""Unit tests for inbound prompt queuing when Antigravity is busy."""

from unittest.mock import AsyncMock, MagicMock
import pytest

from agybot.handlers.inbound_queue import (
    clear_inbound_queue,
    dispatch_next_queued_prompt,
    get_queued_prompts,
    has_queued_inbound_prompts,
    is_thread_busy,
    pop_next_inbound_prompt,
    queue_inbound_prompt,
)
from agybot.handlers.message_queue import (
    clear_active_tool,
    set_active_tool,
)


@pytest.fixture(autouse=True)
def clean_queue_state():
    clear_inbound_queue(1227075616, 178763)
    clear_active_tool(1227075616, 178763)
    yield
    clear_inbound_queue(1227075616, 178763)
    clear_active_tool(1227075616, 178763)


def test_inbound_queue_lifecycle():
    user_id = 1227075616
    thread_id = 178763

    assert not has_queued_inbound_prompts(user_id, thread_id)
    assert not is_thread_busy(user_id, thread_id)

    # Queue first prompt
    pos1 = queue_inbound_prompt(user_id, thread_id, "prompt 1")
    assert pos1 == 1
    assert has_queued_inbound_prompts(user_id, thread_id)
    assert is_thread_busy(user_id, thread_id)

    # Queue second prompt
    pos2 = queue_inbound_prompt(user_id, thread_id, "prompt 2")
    assert pos2 == 2

    prompts = get_queued_prompts(user_id, thread_id)
    assert len(prompts) == 2
    assert prompts[0].prompt_text == "prompt 1"
    assert prompts[1].prompt_text == "prompt 2"

    # Pop next prompt
    p1 = pop_next_inbound_prompt(user_id, thread_id)
    assert p1 is not None
    assert p1.prompt_text == "prompt 1"
    assert len(get_queued_prompts(user_id, thread_id)) == 1

    # Clear queue
    cleared = clear_inbound_queue(user_id, thread_id)
    assert cleared == 1
    assert not has_queued_inbound_prompts(user_id, thread_id)


def test_is_thread_busy_reflects_active_tool():
    user_id = 1227075616
    thread_id = 178763

    assert not is_thread_busy(user_id, thread_id)

    set_active_tool(user_id, thread_id, "Running command…", "git status")
    assert is_thread_busy(user_id, thread_id)

    clear_active_tool(user_id, thread_id)
    assert not is_thread_busy(user_id, thread_id)


@pytest.mark.asyncio
async def test_handle_inbound_message_queues_when_busy(monkeypatch):
    from agybot.bot import handle_inbound_message
    from agybot.session import session_manager
    from agybot.tmux_manager import tmux_manager

    user_id = 1227075616
    thread_id = 178763
    wid = "@1"

    session_manager.thread_bindings[thread_id] = wid
    set_active_tool(user_id, thread_id, "Working…", "")

    mock_send_keys = AsyncMock()
    monkeypatch.setattr(tmux_manager, "send_keys", mock_send_keys)

    mock_msg = MagicMock(
        message_id=50,
        chat_id=user_id,
        text="New follow-up task",
        caption=None,
        photo=None,
        document=None,
        message_thread_id=thread_id,
    )
    mock_reply = AsyncMock()
    mock_msg.reply_text = mock_reply

    mock_update = MagicMock(
        effective_user=MagicMock(id=user_id),
        message=mock_msg,
        effective_message=mock_msg,
    )
    mock_context = MagicMock()

    await handle_inbound_message(mock_update, mock_context)

    # Must NOT have sent keys to tmux for running session
    mock_send_keys.assert_not_called()

    # Must have queued the prompt
    assert has_queued_inbound_prompts(user_id, thread_id)
    queued = get_queued_prompts(user_id, thread_id)
    assert len(queued) == 1
    assert queued[0].prompt_text == "New follow-up task"

    # Must have replied with Queued notice
    mock_reply.assert_called_once()
    assert "Queued" in mock_reply.call_args[0][0]


@pytest.mark.asyncio
async def test_dispatch_next_queued_prompt(monkeypatch):
    from agybot.handlers.message_queue import (
        _user_queues,
        get_active_tool,
    )
    from agybot.session import session_manager
    from agybot.tmux_manager import tmux_manager

    user_id = 1227075616
    thread_id = 178763
    wid = "@1"

    session_manager.thread_bindings[thread_id] = wid
    session_manager.group_chat_ids[f"{user_id}:{thread_id}"] = user_id

    queue_inbound_prompt(user_id, thread_id, "Next queued instruction")

    mock_send_keys = AsyncMock(return_value=True)
    monkeypatch.setattr(tmux_manager, "send_keys", mock_send_keys)

    mock_bot = MagicMock()
    mock_bot.send_message = AsyncMock()

    dispatched = await dispatch_next_queued_prompt(mock_bot, user_id, thread_id)
    assert dispatched is True

    # Prompt was dequeued and sent to tmux
    mock_send_keys.assert_called_once_with(wid, "Next queued instruction", enter=True)
    assert not has_queued_inbound_prompts(user_id, thread_id)

    # Active tool state was set to working
    ctx = get_active_tool(user_id, thread_id)
    assert ctx is not None
    assert ctx.action == "Working…"

    # Status task was enqueued in user queue
    assert user_id in _user_queues
    task = _user_queues[user_id].get_nowait()
    assert task.task_type == "status"
    assert "Working…" in task.status_text
