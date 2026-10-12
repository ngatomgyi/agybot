"""Tests for model info and token usage functionality."""

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agybot.bot import usage_command
from agybot.session import (
    AgySession,
    get_configured_model,
    get_live_session_model_info,
    get_session_token_usage,
    session_manager,
)
from agybot.utils import format_token_count


def test_format_token_count():
    assert format_token_count(0) == "0"
    assert format_token_count(-10) == "0"
    assert format_token_count(540) == "540"
    assert format_token_count(999) == "999"
    assert format_token_count(1000) == "1.0k (1,000)"
    assert format_token_count(454741) == "454.7k (454,741)"
    assert format_token_count(4337401) == "4.34M (4,337,401)"
    assert format_token_count(104349687) == "104.35M (104,349,687)"
    assert format_token_count(2500000000) == "2.50B (2,500,000,000)"


def test_get_session_token_usage_missing_or_empty(tmp_path: Path):
    missing_file = tmp_path / "does_not_exist.jsonl"
    totals = get_session_token_usage(missing_file)
    assert totals == {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "total_tokens": 0,
    }

    empty_file = tmp_path / "empty.jsonl"
    empty_file.write_text("", encoding="utf-8")
    totals = get_session_token_usage(empty_file)
    assert totals == {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "total_tokens": 0,
    }


def test_get_session_token_usage_valid_and_corrupt_lines(tmp_path: Path):
    transcript_file = tmp_path / "transcript.jsonl"
    lines = [
        json.dumps({"type": "USER_INPUT", "content": "Hello"}),
        "Corrupted not json line with token in it",
        json.dumps(
            {
                "type": "PLANNER_RESPONSE",
                "input_tokens": 1000,
                "output_tokens": 200,
                "cache_read_tokens": 500,
            }
        ),
        json.dumps({"type": "TOOL_CALL", "tool": "run_command"}),
        json.dumps(
            {
                "type": "PLANNER_RESPONSE",
                "input_tokens": 1500,
                "output_tokens": 300,
                "cache_read_tokens": 1200,
            }
        ),
    ]
    transcript_file.write_text("\n".join(lines), encoding="utf-8")

    totals = get_session_token_usage(transcript_file)
    assert totals["input_tokens"] == 2500
    assert totals["output_tokens"] == 500
    assert totals["cache_read_tokens"] == 1700
    assert totals["total_tokens"] == 4700


def test_get_configured_model(tmp_path: Path, monkeypatch):
    from agybot.config import config

    settings_file = tmp_path / "settings.json"
    settings_file.write_text(
        json.dumps(
            {
                "model": "Claude Sonnet 4.6 (Thinking)",
                "agentMode": "plan",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "agy_data_dir", tmp_path)

    model, mode = get_configured_model()
    assert model == "Claude Sonnet 4.6 (Thinking)"
    assert mode == "plan"


@pytest.mark.asyncio
async def test_get_live_session_model_info_from_pane():
    with patch("agybot.session.tmux_manager.capture_pane") as mock_cap:
        mock_cap.return_value = (
            "Some output\n"
            "esc to cancel                             accept-edits · Gemini 3.8 Flash · high"
        )
        info = await get_live_session_model_info("@1")
        assert info["model"] == "Gemini 3.8 Flash"
        assert info["agent_mode"] == "accept-edits"
        assert info["effort"] == "high"


@pytest.mark.asyncio
async def test_get_live_session_model_info_fallback_settings(
    tmp_path: Path, monkeypatch
):
    from agybot.config import config

    settings_file = tmp_path / "settings.json"
    settings_file.write_text(
        json.dumps(
            {
                "model": "GPT-OSS 120B (Medium)",
                "agentMode": "accept-edits",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "agy_data_dir", tmp_path)

    with patch("agybot.session.tmux_manager.capture_pane") as mock_cap:
        mock_cap.return_value = "No status bar here"
        info = await get_live_session_model_info("@2")
        assert info["model"] == "GPT-OSS 120B (Medium)"
        assert info["agent_mode"] == "accept-edits"


@pytest.mark.asyncio
async def test_usage_command_with_session(tmp_path: Path, monkeypatch):
    from agybot.config import config

    monkeypatch.setattr(config, "allowed_users", [12345])

    transcript_file = tmp_path / "transcript.jsonl"
    transcript_file.write_text(
        json.dumps(
            {
                "input_tokens": 10000,
                "output_tokens": 500,
                "cache_read_tokens": 20000,
            }
        ),
        encoding="utf-8",
    )

    mock_session = AgySession(
        session_id="test-conv-1234",
        summary="Test Conversation",
        message_count=15,
        file_path=str(transcript_file),
        cwd="/root/project",
    )

    session_manager.bind_thread(101, "@1")

    update = MagicMock()
    update.effective_user.id = 12345
    update.effective_message.message_thread_id = 101
    update.message = MagicMock()

    context = MagicMock()

    with (
        patch.object(
            session_manager,
            "resolve_session_for_window",
            AsyncMock(return_value=mock_session),
        ),
        patch(
            "agybot.bot.get_live_session_model_info",
            AsyncMock(
                return_value={
                    "model": "Gemini 3.8 Flash",
                    "agent_mode": "accept-edits",
                    "effort": "high",
                }
            ),
        ),
        patch("agybot.bot.safe_reply", AsyncMock()) as mock_safe_reply,
    ):
        await usage_command(update, context)

        mock_safe_reply.assert_called_once()
        args = mock_safe_reply.call_args[0]
        text = args[1]

        assert "Session & Model Usage" in text
        assert "Gemini 3.8 Flash (High)" in text
        assert "accept-edits" in text
        assert "10.0k (10,000)" in text
        assert "500" in text
        assert "20.0k (20,000)" in text
        assert "30.5k (30,500)" in text
        assert "15" in text
        assert "Test Conversation" in text
        assert "/root/project" in text
        assert "test-conv-1234" in text


@pytest.mark.asyncio
async def test_model_command_no_args(monkeypatch):
    from agybot.bot import model_command
    from agybot.config import config

    monkeypatch.setattr(config, "allowed_users", [12345])
    session_manager.bind_thread(101, "@1")

    update = MagicMock()
    update.effective_user.id = 12345
    update.effective_message.message_thread_id = 101
    update.message = MagicMock()

    context = MagicMock()
    context.args = []

    with (
        patch(
            "agybot.bot.get_live_session_model_info",
            AsyncMock(
                return_value={
                    "model": "Gemini 3.8 Flash",
                    "agent_mode": "accept-edits",
                    "effort": "medium",
                }
            ),
        ),
        patch("agybot.bot.safe_reply", AsyncMock()) as mock_safe_reply,
    ):
        await model_command(update, context)

        mock_safe_reply.assert_called_once()
        text = mock_safe_reply.call_args[0][1]
        kwargs = mock_safe_reply.call_args[1]

        assert "Active Model & Reasoning Effort" in text
        assert "Gemini 3.8 Flash" in text
        assert "Medium" in text
        assert "reply_markup" in kwargs
        assert kwargs["reply_markup"] is not None


@pytest.mark.asyncio
async def test_model_command_with_alias(monkeypatch):
    from agybot.bot import model_command
    from agybot.config import config

    monkeypatch.setattr(config, "allowed_users", [12345])
    session_manager.bind_thread(101, "@1")

    update = MagicMock()
    update.effective_user.id = 12345
    update.effective_message.message_thread_id = 101
    update.message = MagicMock()

    context = MagicMock()
    context.args = ["medium"]

    with (
        patch(
            "agybot.bot.tmux_manager.send_keys", AsyncMock()
        ) as mock_send_keys,
        patch(
            "agybot.bot.get_live_session_model_info",
            AsyncMock(
                return_value={
                    "model": "Gemini 3.8 Flash",
                    "agent_mode": "accept-edits",
                    "effort": "medium",
                }
            ),
        ),
        patch("agybot.bot.safe_reply", AsyncMock()) as mock_safe_reply,
    ):
        await model_command(update, context)

        mock_send_keys.assert_called_once_with(
            "@1", "/model gemini-3.8-flash-medium", enter=True
        )
        mock_safe_reply.assert_called_once()
        text = mock_safe_reply.call_args[0][1]
        assert "Model Updated Successfully" in text
        assert "Medium" in text


@pytest.mark.asyncio
async def test_callback_handler_model_selection(monkeypatch):
    from agybot.bot import CB_MODEL_PREFIX, callback_handler
    from agybot.config import config

    monkeypatch.setattr(config, "allowed_users", [12345])

    update = MagicMock()
    update.effective_user.id = 12345
    query = MagicMock()
    query.from_user.id = 12345
    query.data = f"{CB_MODEL_PREFIX}gemini-3.8-flash-low:@1"
    query.answer = AsyncMock()
    query.message = MagicMock()
    update.callback_query = query

    context = MagicMock()

    with (
        patch(
            "agybot.bot.tmux_manager.send_keys", AsyncMock()
        ) as mock_send_keys,
        patch(
            "agybot.bot.get_live_session_model_info",
            AsyncMock(
                return_value={
                    "model": "Gemini 3.8 Flash",
                    "agent_mode": "accept-edits",
                    "effort": "low",
                }
            ),
        ),
        patch("agybot.bot.safe_edit", AsyncMock()) as mock_safe_edit,
    ):
        await callback_handler(update, context)

        query.answer.assert_called_once_with("Switching model...")
        mock_send_keys.assert_called_once_with(
            "@1", "/model gemini-3.8-flash-low", enter=True
        )
        mock_safe_edit.assert_called_once()
        text = mock_safe_edit.call_args[0][1]
        assert "Model Updated Successfully" in text
        assert "Low" in text


def test_render_quota_progress_bar():
    from agybot.session import render_quota_progress_bar

    assert render_quota_progress_bar(0.0, 10) == "░░░░░░░░░░"
    assert render_quota_progress_bar(1.0, 10) == "██████████"
    assert render_quota_progress_bar(0.5, 10) == "█████░░░░░"
    assert render_quota_progress_bar(-0.2, 10) == "░░░░░░░░░░"
    assert render_quota_progress_bar(1.5, 10) == "██████████"


def test_format_quota_refresh_time():
    from datetime import datetime, timedelta, timezone
    from agybot.session import format_quota_refresh_time

    assert format_quota_refresh_time("") == ""
    # 2 hours ahead
    future_2h = datetime.now(timezone.utc) + timedelta(hours=2, minutes=15)
    res = format_quota_refresh_time(future_2h.isoformat())
    assert "2h 1" in res or "2h 15m" in res or "2h 14m" in res

    # Past time
    past = datetime.now(timezone.utc) - timedelta(minutes=5)
    assert format_quota_refresh_time(past.isoformat()) == "Refreshes shortly"


def test_parse_quota_from_pane():
    from agybot.session import parse_quota_from_pane

    pane_text = (
        "└ Models & Quota\n\n"
        "  Account: user@example.com\n\n"
        "GEMINI MODELS\n"
        "  Models within this group: Gemini Flash, Gemini Pro\n\n"
        "  Weekly Limit Remaining\n"
        "    [████████████████████░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░] 40.00%\n"
        "    Refreshes in 71h 14m\n\n"
        "  Five Hour Limit Remaining\n"
        "    [██████████████████████████████████░░░░░░░░░░░░░░░░] 68.00%\n"
        "    Refreshes in 3h 36m\n"
    )

    groups = parse_quota_from_pane(pane_text)
    assert len(groups) == 1
    g = groups[0]
    assert g["group"] == "GEMINI MODELS"
    assert "Gemini Flash" in g["desc"]
    assert len(g["buckets"]) == 2
    assert g["buckets"][0]["name"] == "Weekly Limit Remaining"
    assert g["buckets"][0]["percentage"] == "40.00%"
    assert g["buckets"][0]["refresh"] == "Refreshes in 71h 14m"


def test_format_quota_markdown():
    from agybot.session import format_quota_markdown

    groups = [
        {
            "group": "Gemini Models",
            "desc": "Models within this group: Gemini Flash, Gemini Pro",
            "buckets": [
                {
                    "name": "Weekly Limit Remaining",
                    "percentage": "40.0%",
                    "bar": "██████░░░░░░░░░",
                    "refresh": "Refreshes in 71h 14m",
                }
            ],
        }
    ]

    md = format_quota_markdown(groups)
    assert "Usage Limits & Quota Remaining" in md
    assert "Gemini Models" in md
    assert "Gemini Flash, Gemini Pro" in md
    assert "Weekly Limit" in md
    assert "40.0%" in md
    assert "Refreshes in 71h 14m" in md


@pytest.mark.asyncio
async def test_usage_command_with_quota(tmp_path: Path, monkeypatch):
    from agybot.bot import usage_command
    from agybot.config import config

    monkeypatch.setattr(config, "allowed_users", [12345])
    session_manager.bind_thread(101, "@1")

    transcript_file = tmp_path / "transcript.jsonl"
    transcript_file.write_text("{}", encoding="utf-8")

    mock_session = AgySession(
        session_id="test-conv-quota",
        summary="Test Session",
        message_count=5,
        file_path=str(transcript_file),
        cwd="/root/agybot",
    )

    update = MagicMock()
    update.effective_user.id = 12345
    update.effective_message.message_thread_id = 101
    update.message = MagicMock()

    context = MagicMock()

    mock_quota_groups = [
        {
            "group": "Gemini Models",
            "desc": "Models within this group: Gemini Flash, Gemini Pro",
            "buckets": [
                {
                    "name": "Weekly Limit Remaining",
                    "percentage": "39.8%",
                    "bar": "██████░░░░░░░░░",
                    "refresh": "Refreshes in 71h 11m",
                }
            ],
        }
    ]

    with (
        patch.object(
            session_manager,
            "resolve_session_for_window",
            AsyncMock(return_value=mock_session),
        ),
        patch(
            "agybot.bot.get_live_session_model_info",
            AsyncMock(
                return_value={
                    "model": "Gemini 3.8 Flash",
                    "agent_mode": "accept-edits",
                    "effort": "medium",
                }
            ),
        ),
        patch(
            "agybot.bot.get_antigravity_quota_info",
            AsyncMock(return_value=mock_quota_groups),
        ),
        patch("agybot.bot.safe_reply", AsyncMock()) as mock_safe_reply,
    ):
        await usage_command(update, context)

        mock_safe_reply.assert_called_once()
        text = mock_safe_reply.call_args[0][1]
        assert "Session & Model Usage" in text
        assert "Usage Limits & Quota Remaining" in text
        assert "Gemini Models" in text
        assert "39.8%" in text


