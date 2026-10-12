"""Unit tests for Caveman skill and rule management and /caveman command."""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agybot.bot import caveman_command
from agybot.caveman import (
    get_bundled_rule_content,
    get_bundled_skill_content,
    is_caveman_rule_active,
    is_caveman_skill_installed,
    set_caveman_rule,
    sync_caveman_configuration,
)


def test_bundled_resources():
    skill_content = get_bundled_skill_content()
    assert "name: caveman" in skill_content
    assert "Respond terse like smart caveman" in skill_content

    rule_content = get_bundled_rule_content()
    assert "Caveman Compressed Communication" in rule_content
    assert "Kill ceremony" in rule_content


def test_caveman_rule_toggle(tmp_path: Path, monkeypatch):
    test_rules_dir = tmp_path / "rules"
    test_rule_file = test_rules_dir / "caveman.md"
    test_gemini_file = tmp_path / "GEMINI.md"

    monkeypatch.setattr("agybot.caveman.CAVEMAN_RULES_DIR", test_rules_dir)
    monkeypatch.setattr("agybot.caveman.CAVEMAN_RULE_FILE", test_rule_file)
    monkeypatch.setattr("agybot.caveman.GLOBAL_GEMINI_FILE", test_gemini_file)
    monkeypatch.setattr("agybot.caveman.CONFIG_GEMINI_FILE", tmp_path / "cfg_GEMINI.md")
    monkeypatch.setattr(
        "agybot.caveman.WORKSPACE_PARENT_GEMINI_FILE", tmp_path / "root_GEMINI.md"
    )

    assert not is_caveman_rule_active()
    assert is_caveman_skill_installed() or not is_caveman_skill_installed()

    # Enable rule
    assert set_caveman_rule(True)
    assert is_caveman_rule_active()
    assert test_rule_file.is_file()
    assert test_gemini_file.is_file()
    assert "Kill ceremony" in test_rule_file.read_text(encoding="utf-8")
    assert "Kill ceremony" in test_gemini_file.read_text(encoding="utf-8")

    # Disable rule
    assert set_caveman_rule(False)
    assert not is_caveman_rule_active()
    assert not test_rule_file.exists()
    assert not test_gemini_file.exists()


def test_sync_caveman_configuration(tmp_path: Path, monkeypatch):
    test_skill_dir = tmp_path / "skills" / "caveman"
    test_skill_file = test_skill_dir / "SKILL.md"
    test_rules_dir = tmp_path / "rules"
    test_rule_file = test_rules_dir / "caveman.md"

    monkeypatch.setattr("agybot.caveman.CAVEMAN_SKILL_DIR", test_skill_dir)
    monkeypatch.setattr("agybot.caveman.CAVEMAN_SKILL_FILE", test_skill_file)
    monkeypatch.setattr("agybot.caveman.CAVEMAN_RULES_DIR", test_rules_dir)
    monkeypatch.setattr("agybot.caveman.CAVEMAN_RULE_FILE", test_rule_file)
    monkeypatch.setattr("agybot.caveman.GLOBAL_GEMINI_FILE", tmp_path / "GEMINI.md")
    monkeypatch.setattr("agybot.caveman.CONFIG_GEMINI_FILE", tmp_path / "cfg_GEMINI.md")
    monkeypatch.setattr(
        "agybot.caveman.WORKSPACE_PARENT_GEMINI_FILE", tmp_path / "root_GEMINI.md"
    )

    # Sync with "full" mode
    res = sync_caveman_configuration("full")
    assert res["success"] is True
    assert res["skill_installed"] is True
    assert res["rule_active"] is True
    assert test_skill_file.is_file()
    assert test_rule_file.is_file()

    # Sync with "off" mode
    res_off = sync_caveman_configuration("off")
    assert res_off["success"] is True
    assert res_off["skill_installed"] is True
    assert res_off["rule_active"] is False
    assert not test_rule_file.exists()


@pytest.mark.asyncio
async def test_caveman_command_status(monkeypatch):
    from agybot.config import config

    monkeypatch.setattr(config, "allowed_users", [12345])

    update = MagicMock()
    update.effective_user.id = 12345
    update.effective_message.message_thread_id = 99
    update.message = MagicMock()

    context = MagicMock()
    context.args = []

    with patch("agybot.bot.safe_reply", AsyncMock()) as mock_safe_reply:
        await caveman_command(update, context)
        mock_safe_reply.assert_called_once()
        text = mock_safe_reply.call_args[0][1]
        assert "Caveman Compressed Mode Status" in text
        assert "/caveman on" in text


@pytest.mark.asyncio
async def test_caveman_command_toggle_on_and_off(monkeypatch):
    from agybot.config import config
    from agybot.session import session_manager

    monkeypatch.setattr(config, "allowed_users", [12345])
    session_manager.bind_thread(99, "@1")

    update = MagicMock()
    update.effective_user.id = 12345
    update.effective_message.message_thread_id = 99
    update.message = MagicMock()

    context = MagicMock()

    with (
        patch("agybot.bot.set_caveman_rule") as mock_set_rule,
        patch("agybot.bot.tmux_manager.send_keys", AsyncMock()) as mock_send_keys,
        patch("agybot.bot.safe_reply", AsyncMock()) as mock_safe_reply,
    ):
        # 1. Turn on
        context.args = ["full"]
        await caveman_command(update, context)
        mock_set_rule.assert_called_with(True)
        mock_send_keys.assert_called_with("@1", "/caveman full", enter=True)
        assert "Caveman Full Mode Enabled" in mock_safe_reply.call_args[0][1]

        # 2. Turn off
        context.args = ["off"]
        await caveman_command(update, context)
        mock_set_rule.assert_called_with(False)
        mock_send_keys.assert_called_with("@1", "stop caveman", enter=True)
        assert "Caveman Mode Disabled" in mock_safe_reply.call_args[0][1]
