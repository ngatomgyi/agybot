# agybot

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python: >=3.12](https://img.shields.io/badge/Python-3.12+-blue.svg)](https://python.org)
[![Package Manager: uv](https://img.shields.io/badge/package%20manager-uv-blueviolet)](https://astral.sh/uv)
[![Code Style: Ruff](https://img.shields.io/badge/code%20style-ruff-000000.svg)](https://github.com/astral-sh/ruff)

A Telegram bridge connecting Telegram forum topics and direct bot threads to autonomous [Google Antigravity (`agy`)](https://github.com/google/antigravity) CLI sessions in tmux.

Each topic or thread is bound 1:1 to a dedicated tmux window running an autonomous Antigravity instance.

---

## ✨ Features

- **1 Topic = 1 Window = 1 Session**: 1:1 mapping between Telegram topics and autonomous `agy` workspace windows.
- **Autonomous Execution**: Automatically launches windows with `agy --mode accept-edits --dangerously-skip-permissions` for seamless autonomous coding.
- **Direct Thread & Forum Support**: Works in 1-on-1 private chat with Telegram Thread Mode enabled or inside Forum Supergroups.
- **Inbound Prompt Queuing**: Queue follow-up prompts anytime while the agent is busy without interrupting active tasks.
- **Job-Specific Live Status**: Real-time semantic status updates (`⚙️` commands, `🔍` reads, `✏️` edits, `🌐` web, `💭` thinking) that automatically clean up when finished.
- **Collapsible In-Place Thoughts**: Model reasoning blocks are consolidated into an expandable blockquote per turn and automatically removed upon final response delivery.
- **Interactive Terminal UI**: In-line keyboard support for Antigravity choices, confirmations, and question prompts.
- **Artifact & Build Delivery**: Automatically delivers generated markdown documents and build binaries (`.apk`, `.aab`, `.ipa` up to 48 MB).
- **Terminal Screenshots**: Render high-fidelity dark-themed terminal screenshots (`/screenshot`) with ANSI colors and refresh buttons.
- **Caveman Mode**: Built-in `/caveman` token-saving mode to compress responses and reasoning output.

---

## 🚀 Quickstart

### Prerequisites

- Linux OS (Ubuntu 22.04+, Debian 12+, or modern distro)
- [tmux](https://github.com/tmux/tmux) (`sudo apt install tmux`)
- [uv](https://astral.sh/uv) (`curl -LsSf https://astral.sh/uv/install.sh | sh`)
- Python 3.12+
- Google Antigravity CLI (`agy`) installed and in `PATH`
- Telegram Bot Token from [@BotFather](https://t.me/BotFather)

### Interactive Deployment (Recommended)

```bash
git clone https://github.com/ngatomgyi/agybot.git
cd agybot
bash deploy.sh
```

`deploy.sh` checks dependencies, interactively configures tokens and user IDs, sets up Caveman mode, configures Python virtual environments via `uv`, and optionally installs a `systemd` background service.

### Manual Setup

```bash
# 1. Clone repository
git clone https://github.com/ngatomgyi/agybot.git
cd agybot

# 2. Sync dependencies
uv sync

# 3. Configure environment
cp .env.example .env
# Edit TELEGRAM_BOT_TOKEN and ALLOWED_USERS in .env

# 4. Start agybot
uv run agybot
```

---

## 📱 Telegram Commands

| Command | Description |
| :--- | :--- |
| `/start` | Show welcome guide and setup instructions |
| `/usage` | Show active model, reasoning effort, token breakdown, and stats |
| `/screenshot` | Capture current terminal screen as an image |
| `/artifacts` | Download generated markdown and build binaries |
| `/history` | Browse paginated conversation history |
| `/queue` | View prompts currently queued for this topic |
| `/clearqueue` | Clear queued prompts for this topic |
| `/esc` | Interrupt the active agent turn |
| `/clear` | Clear session conversation context |
| `/caveman` | Toggle or check Caveman token-saving mode (`on`, `off`, `full`) |
| `/unbind` | Unbind topic from tmux window |
| `/kill` | Terminate the bound tmux window and unbind topic |

---

## ⚙️ Configuration

Options are configured in `.env` (or `~/.agybot/.env`):

| Variable | Default | Description |
| :--- | :--- | :--- |
| `TELEGRAM_BOT_TOKEN` | *Required* | Telegram Bot API token from [@BotFather](https://t.me/BotFather) |
| `ALLOWED_USERS` | *Required* | Comma-separated list of allowed Telegram user IDs |
| `TMUX_SESSION_NAME` | `agybot` | Target tmux session name |
| `AGY_COMMAND` | `agy --mode accept-edits ...` | Antigravity CLI launch command |
| `CAVEMAN_DEFAULT_MODE` | `full` | Default Caveman mode (`full` or `off`) |
| `MONITOR_POLL_INTERVAL` | `1.5` | Transcript polling interval in seconds |
| `AGYBOT_SHOW_TOOL_CALLS` | `true` | Show tool execution status updates |
| `AGYBOT_SHOW_THINKING` | `true` | Stream model chain-of-thought blocks |
| `AGYBOT_AUTO_RENAME_TOPICS`| `true` | Automatically rename Telegram topic to workspace directory |

---

## 🛠️ Service Management

If installed as a systemd service:

```bash
sudo systemctl status agybot
sudo systemctl restart agybot
journalctl -u agybot -f
```

---

## 🧪 Development

```bash
# Run test suite
uv run pytest

# Lint and formatting checks
uv run ruff check
uv run ruff format --check
```

---

## 📄 License

Distributed under the [MIT License](LICENSE).
