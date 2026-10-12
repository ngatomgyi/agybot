"""Configuration management for agybot.

Loads settings from environment variables and .env files:
  1. Local .env in current working directory (highest priority)
  2. ~/.agybot/.env (fallback)

Key class: Config (singleton instantiated as `config`).
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import ClassVar

from dotenv import load_dotenv

from .utils import agybot_dir

logger = logging.getLogger(__name__)

# Sensitive env vars to scrub from tmux sessions to prevent leaking secrets to child processes
SENSITIVE_ENV_VARS: set[str] = {
    "TELEGRAM_BOT_TOKEN",
    "BOT_TOKEN",
    "ALLOWED_USERS",
}


class Config:
    """Configuration singleton for agybot."""

    _instance: ClassVar[Config | None] = None

    def __new__(cls) -> Config:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._init()
        return cls._instance

    def _init(self) -> None:
        """Initialize configuration from environment and .env files."""
        self.config_dir = agybot_dir()
        self.config_dir.mkdir(parents=True, exist_ok=True)

        # 1. Config dir .env (base)
        config_env = self.config_dir / ".env"
        if config_env.exists():
            load_dotenv(config_env)

        # 2. Local .env (override)
        local_env = Path.cwd() / ".env"
        if local_env.exists() and local_env.resolve() != config_env.resolve():
            load_dotenv(local_env, override=True)

        # Telegram credentials
        self.telegram_bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
        if not self.telegram_bot_token:
            raise ValueError("TELEGRAM_BOT_TOKEN environment variable is required")

        allowed_users_str = os.getenv("ALLOWED_USERS", "")
        if not allowed_users_str:
            raise ValueError("ALLOWED_USERS environment variable is required")

        try:
            self.allowed_users = [
                int(uid.strip()) for uid in allowed_users_str.split(",") if uid.strip()
            ]
        except ValueError as err:
            raise ValueError(
                f"ALLOWED_USERS must be comma-separated integers, got: {allowed_users_str}"
            ) from err

        if not self.allowed_users:
            raise ValueError("ALLOWED_USERS must contain at least one valid user ID")

        # Tmux session name and main window
        self.tmux_session_name = os.getenv("TMUX_SESSION_NAME", "agybot")
        self.tmux_main_window_name = "__main__"

        # Antigravity command to execute in new windows
        # Defaults to accept-edits mode with yolo permissions auto-approval
        self.agy_command = os.getenv(
            "AGY_COMMAND",
            "agy --mode accept-edits --dangerously-skip-permissions",
        )

        # Antigravity data directory detection
        custom_agy_dir = os.getenv("AGY_DATA_DIR")
        if custom_agy_dir:
            self.agy_data_dir = Path(custom_agy_dir)
        elif (Path.home() / ".gemini" / "antigravity-cli").exists():
            self.agy_data_dir = Path.home() / ".gemini" / "antigravity-cli"
        elif (Path.home() / ".gemini" / "antigravity").exists():
            self.agy_data_dir = Path.home() / ".gemini" / "antigravity"
        else:
            self.agy_data_dir = Path.home() / ".gemini" / "antigravity-cli"

        self.agy_brain_path = self.agy_data_dir / "brain"
        self.agy_db_path = self.agy_data_dir / "conversation_summaries.db"
        self.agy_history_file = self.agy_data_dir / "history.jsonl"
        self.agy_logs_dir = self.agy_data_dir / "log"
        self.agy_crashes_dir = self.agy_data_dir / "crashes"

        # State files under ~/.agybot
        self.state_file = self.config_dir / "state.json"
        self.session_map_file = self.config_dir / "session_map.json"
        self.monitor_state_file = self.config_dir / "monitor_state.json"

        # Polling intervals
        self.monitor_poll_interval = float(os.getenv("MONITOR_POLL_INTERVAL", "1.5"))

        # Display preferences
        self.show_user_messages = (
            os.getenv("AGYBOT_SHOW_USER_MESSAGES", "false").lower() == "true"
        )
        self.show_tool_calls = (
            os.getenv("AGYBOT_SHOW_TOOL_CALLS", "false").lower() == "true"
        )
        self.show_thinking = (
            os.getenv("AGYBOT_SHOW_THINKING", "false").lower() == "true"
        )
        self.show_hidden_dirs = (
            os.getenv("AGYBOT_SHOW_HIDDEN_DIRS", "false").lower() == "true"
        )
        self.auto_rename_topics = (
            os.getenv("AGYBOT_AUTO_RENAME_TOPICS", "true").lower() == "true"
        )


config = Config.__new__(Config)
