"""Application entry point for agybot.

Bootstraps logging, initializes the agybot tmux session,
and starts the Telegram bot polling loop.
"""

from __future__ import annotations

import logging
import sys


def main() -> None:
    """Main application entry point."""
    if len(sys.argv) > 1:
        if sys.argv[1] in ("-h", "--help"):
            print("Usage: agybot    Start the Antigravity Telegram bridge bot")
            return
        if sys.argv[1] in ("-v", "--version"):
            from . import __version__

            print(f"agybot version {__version__}")
            return
        print(f"Unknown argument: {sys.argv[1]}", file=sys.stderr)
        sys.exit(2)

    logging.basicConfig(
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        level=logging.WARNING,
    )

    try:
        from .config import config
    except ValueError as e:
        from .utils import agybot_dir

        env_path = agybot_dir() / ".env"
        print(f"\n❌ Configuration Error: {e}\n", file=sys.stderr)
        print(f"Please configure {env_path} or run ./deploy.sh\n", file=sys.stderr)
        print("Required values:")
        print("  TELEGRAM_BOT_TOKEN=your_bot_token_here")
        print("  ALLOWED_USERS=your_telegram_user_id\n")
        sys.exit(1)

    logging.getLogger("agybot").setLevel(logging.INFO)
    logging.getLogger("telegram.ext.AIORateLimiter").setLevel(logging.INFO)
    logger = logging.getLogger(__name__)

    from .tmux_manager import tmux_manager

    logger.info("Initializing agybot...")
    logger.info("Allowed users: %s", config.allowed_users)
    logger.info("Antigravity command: %s", config.agy_command)
    logger.info("Data directory: %s", config.agy_data_dir)

    # Ensure tmux session exists
    session = tmux_manager.get_or_create_session()
    logger.info("Tmux session '%s' ready", session.session_name)

    logger.info("Starting Telegram bot polling...")
    from .bot import create_bot

    application = create_bot()
    application.run_polling(allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    main()
