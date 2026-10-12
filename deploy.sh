#!/usr/bin/env bash
# ==============================================================================
#  agybot — One-Click Interactive Deployment Script
#  Bridges Telegram Forum Topics to Antigravity (agy) CLI Sessions via Tmux
# ==============================================================================

set -euo pipefail

# --- Color & Style Definitions ---
BOLD="\033[1m"
DIM="\033[2m"
CYAN="\033[36m"
GREEN="\033[32m"
YELLOW="\033[33m"
RED="\033[31m"
BLUE="\033[34m"
MAGENTA="\033[35m"
RESET="\033[0m"

INFO="${CYAN}ℹ${RESET}"
SUCCESS="${GREEN}✔${RESET}"
WARN="${YELLOW}⚠${RESET}"
ERROR="${RED}✖${RESET}"
ARROW="${MAGENTA}❯${RESET}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${SCRIPT_DIR}"

clear 2>/dev/null || true

# --- Banner ---
echo -e "${CYAN}${BOLD}"
cat << 'EOF'
    ___   ______  __  __ ____   ____  ______
   /   | / ____/ / / / // __ ) / __ \/_  __/
  / /| |/ / __  / /_/ // __  |/ / / / / /   
 / ___ / /_/ /  \__, // /_/ // /_/ / / /    
/_/  |_\____/  /____//_____/ \____/ /_/     
EOF
echo -e "${RESET}${BOLD} Antigravity (agy) Telegram Bridge Bot — One-Click Deploy${RESET}"
echo -e "${DIM} 1 Topic = 1 Tmux Window = 1 Antigravity Session (accept-edits mode)${RESET}\n"

# ==============================================================================
# Step 1: Check System Dependencies
# ==============================================================================
echo -e "${BOLD}${BLUE}[1/5] Checking System Dependencies...${RESET}"

# 1.1 Check Python
if command -v python3 >/dev/null 2>&1; then
    PY_VER=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
    echo -e " ${SUCCESS} Python detected: ${BOLD}${PY_VER}${RESET}"
else
    echo -e " ${ERROR} Python 3 is required but not installed."
    exit 1
fi

# 1.2 Check tmux
if command -v tmux >/dev/null 2>&1; then
    TMUX_VER=$(tmux -V)
    echo -e " ${SUCCESS} Tmux detected: ${BOLD}${TMUX_VER}${RESET}"
else
    echo -e " ${WARN} Tmux not found. Installing tmux via apt..."
    if [ "$(id -u)" -eq 0 ]; then
        apt-get update -qq && apt-get install -y tmux
    else
        sudo apt-get update -qq && sudo apt-get install -y tmux
    fi
    echo -e " ${SUCCESS} Tmux installed successfully."
fi

# 1.3 Check uv (Python package manager)
if command -v uv >/dev/null 2>&1; then
    UV_VER=$(uv --version)
    echo -e " ${SUCCESS} uv detected: ${BOLD}${UV_VER}${RESET}"
else
    echo -e " ${WARN} uv package manager not found. Installing uv..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="${HOME}/.local/bin:${PATH}"
    echo -e " ${SUCCESS} uv installed successfully."
fi

# 1.4 Check Antigravity CLI (agy)
if command -v agy >/dev/null 2>&1; then
    AGY_PATH=$(which agy)
    echo -e " ${SUCCESS} Antigravity CLI detected: ${BOLD}${AGY_PATH}${RESET}"
else
    echo -e " ${WARN} 'agy' binary not found in PATH."
    if [ -f "${HOME}/.local/bin/agy" ]; then
        export PATH="${HOME}/.local/bin:${PATH}"
        echo -e " ${SUCCESS} Found agy in ~/.local/bin/agy and added to PATH."
    else
        echo -e " ${ERROR} Antigravity CLI ('agy') is required. Please install it first."
        exit 1
    fi
fi

echo ""

# ==============================================================================
# Step 2: Interactive Configuration
# ==============================================================================
echo -e "${BOLD}${BLUE}[2/5] Interactive Configuration${RESET}"
echo -e "${DIM}Configure your Telegram Bot token and authorized user IDs.${RESET}\n"

ENV_FILE="${SCRIPT_DIR}/.env"
GLOBAL_ENV="${HOME}/.agybot/.env"
mkdir -p "${HOME}/.agybot"

# Pre-load existing values if available
EXISTING_TOKEN=""
EXISTING_USERS=""
EXISTING_CMD="agy --mode accept-edits --dangerously-skip-permissions"
EXISTING_TMUX="agybot"

if [ -f "${ENV_FILE}" ]; then
    EXISTING_TOKEN=$(grep -E "^TELEGRAM_BOT_TOKEN=" "${ENV_FILE}" | cut -d'=' -f2- | tr -d '"' || true)
    EXISTING_USERS=$(grep -E "^ALLOWED_USERS=" "${ENV_FILE}" | cut -d'=' -f2- | tr -d '"' || true)
    EXISTING_CMD=$(grep -E "^AGY_COMMAND=" "${ENV_FILE}" | cut -d'=' -f2- | tr -d '"' || echo "${EXISTING_CMD}")
    EXISTING_TMUX=$(grep -E "^TMUX_SESSION_NAME=" "${ENV_FILE}" | cut -d'=' -f2- | tr -d '"' || echo "${EXISTING_TMUX}")
elif [ -f "${HOME}/.ccbot/.env" ]; then
    # Smart import from ccbot if available
    EXISTING_TOKEN=$(grep -E "^TELEGRAM_BOT_TOKEN=" "${HOME}/.ccbot/.env" | cut -d'=' -f2- | tr -d '"' || true)
    EXISTING_USERS=$(grep -E "^ALLOWED_USERS=" "${HOME}/.ccbot/.env" | cut -d'=' -f2- | tr -d '"' || true)
fi

# 2.1 Telegram Bot Token Prompt
while true; do
    if [ -n "${EXISTING_TOKEN}" ]; then
        MASKED_TOKEN="${EXISTING_TOKEN:0:6}...${EXISTING_TOKEN: -4}"
        echo -e "${BOLD}Telegram Bot Token${RESET} ${DIM}[Default: ${MASKED_TOKEN}]${RESET}"
        read -r -p "Enter token (or press Enter to keep): " INPUT_TOKEN
        BOT_TOKEN="${INPUT_TOKEN:-${EXISTING_TOKEN}}"
    else
        echo -e "${BOLD}Telegram Bot Token${RESET} ${DIM}(Obtain from @BotFather)${RESET}"
        read -r -p "Enter token: " BOT_TOKEN
    fi

    if [[ "${BOT_TOKEN}" =~ ^[0-9]{8,12}:[a-zA-Z0-9_-]{30,50}$ ]]; then
        echo -e " ${SUCCESS} Valid token format.\n"
        break
    else
        echo -e " ${WARN} Token doesn't match standard Telegram format (e.g. 123456789:ABCdef...). Try again."
    fi
done

# 2.2 Allowed Users Prompt
while true; do
    if [ -n "${EXISTING_USERS}" ]; then
        echo -e "${BOLD}Allowed Telegram User IDs${RESET} ${DIM}[Default: ${EXISTING_USERS}]${RESET}"
        read -r -p "Enter user IDs (comma-separated, or Enter to keep): " INPUT_USERS
        ALLOWED_USERS="${INPUT_USERS:-${EXISTING_USERS}}"
    else
        echo -e "${BOLD}Allowed Telegram User IDs${RESET} ${DIM}(Get your ID from @userinfobot)${RESET}"
        read -r -p "Enter user ID(s): " ALLOWED_USERS
    fi

    # Remove spaces
    ALLOWED_USERS=$(echo "${ALLOWED_USERS}" | tr -d ' ')
    if [[ "${ALLOWED_USERS}" =~ ^[0-9]+(,[0-9]+)*$ ]]; then
        echo -e " ${SUCCESS} Allowed user(s): ${BOLD}${ALLOWED_USERS}${RESET}\n"
        break
    else
        echo -e " ${WARN} Must be comma-separated numeric IDs. Try again."
    fi
done

# 2.3 Antigravity Command Prompt
DEFAULT_AGY_CMD="agy --mode accept-edits --dangerously-skip-permissions"
echo -e "${BOLD}Antigravity Startup Command${RESET}"
echo -e "${DIM}Starts each new thread window in accept-edits mode with autonomous permissions.${RESET}"
echo -e "${DIM}Default: ${DEFAULT_AGY_CMD}${RESET}"
read -r -p "Command [press Enter for default]: " INPUT_CMD
AGY_COMMAND="${INPUT_CMD:-${DEFAULT_AGY_CMD}}"
echo -e " ${SUCCESS} Execution command set to: ${BOLD}${AGY_COMMAND}${RESET}\n"

# 2.4 Tmux Session Name
echo -e "${BOLD}Tmux Session Name${RESET} ${DIM}[Default: agybot]${RESET}"
read -r -p "Tmux session name: " INPUT_TMUX
TMUX_SESSION_NAME="${INPUT_TMUX:-agybot}"
echo -e " ${SUCCESS} Tmux session: ${BOLD}${TMUX_SESSION_NAME}${RESET}\n"

# 2.5 Display Preferences
echo -e "${BOLD}Display Preferences in Telegram:${RESET}"
read -r -p "Show tool execution calls in chat? (y/N): " SHOW_TOOLS_INPUT
if [[ "${SHOW_TOOLS_INPUT,,}" =~ ^y ]]; then
    SHOW_TOOLS="true"
else
    SHOW_TOOLS="false"
fi

read -r -p "Show model thinking / reasoning blocks in chat? (y/N): " SHOW_THINKING_INPUT
if [[ "${SHOW_THINKING_INPUT,,}" =~ ^y ]]; then
    SHOW_THINKING="true"
else
    SHOW_THINKING="false"
fi

# Write .env file
cat << EOF > "${ENV_FILE}"
# agybot configuration
TELEGRAM_BOT_TOKEN="${BOT_TOKEN}"
ALLOWED_USERS="${ALLOWED_USERS}"
TMUX_SESSION_NAME="${TMUX_SESSION_NAME}"
AGY_COMMAND="${AGY_COMMAND}"
MONITOR_POLL_INTERVAL=1.5
AGYBOT_SHOW_TOOL_CALLS=${SHOW_TOOLS}
AGYBOT_SHOW_THINKING=${SHOW_THINKING}
AGYBOT_SHOW_USER_MESSAGES=false
AGYBOT_AUTO_RENAME_TOPICS=true
EOF

# Sync to ~/.agybot/.env as fallback
cp "${ENV_FILE}" "${GLOBAL_ENV}"
chmod 600 "${ENV_FILE}" "${GLOBAL_ENV}"
echo -e " ${SUCCESS} Configuration saved to ${BOLD}${ENV_FILE}${RESET}\n"

# ==============================================================================
# Step 3: Install Dependencies & Setup Virtual Environment
# ==============================================================================
echo -e "${BOLD}${BLUE}[3/5] Setting Up Python Virtual Environment...${RESET}"
uv sync
echo -e " ${SUCCESS} Dependencies synced and verified via uv.\n"

# ==============================================================================
# Step 4: Systemd Service Installation
# ==============================================================================
echo -e "${BOLD}${BLUE}[4/5] Service Setup & Auto-Start Configuration${RESET}"

SERVICE_FILE="/etc/systemd/system/agybot.service"
CAN_SUDO=false
if [ "$(id -u)" -eq 0 ] || command -v sudo >/dev/null 2>&1; then
    CAN_SUDO=true
fi

if [ "${CAN_SUDO}" = true ]; then
    read -r -p "Do you want to install and enable agybot as a systemd service? (Y/n): " INSTALL_SERVICE_INPUT
    INSTALL_SERVICE="${INSTALL_SERVICE_INPUT:-Y}"

    if [[ "${INSTALL_SERVICE,,}" =~ ^y ]]; then
        echo -e "Creating systemd service file..."
        CURRENT_USER="$(whoami)"
        VENV_BIN="${SCRIPT_DIR}/.venv/bin/agybot"

        SERVICE_CONTENT="[Unit]
Description=Antigravity Telegram Bridge Bot (agybot)
After=network.target

[Service]
Type=simple
User=${CURRENT_USER}
WorkingDirectory=${SCRIPT_DIR}
Environment=\"PATH=${HOME}/.local/bin:/usr/local/bin:/usr/bin:/bin\"
EnvironmentFile=${SCRIPT_DIR}/.env
ExecStart=${VENV_BIN}
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
"

        if [ "$(id -u)" -eq 0 ]; then
            echo "${SERVICE_CONTENT}" > "${SERVICE_FILE}"
            systemctl daemon-reload
            systemctl enable agybot
            systemctl restart agybot
        else
            echo "${SERVICE_CONTENT}" | sudo tee "${SERVICE_FILE}" > /dev/null
            sudo systemctl daemon-reload
            sudo systemctl enable agybot
            sudo systemctl restart agybot
        fi

        echo -e " ${SUCCESS} agybot service installed and started via systemd!"
        if [ "$(id -u)" -eq 0 ]; then
            systemctl status agybot --no-pager || true
        else
            sudo systemctl status agybot --no-pager || true
        fi
        echo ""
    fi
fi

# ==============================================================================
# Step 5: Final Instructions & Verification
# ==============================================================================
echo -e "${BOLD}${GREEN}==============================================================================${RESET}"
echo -e "${BOLD}${GREEN} 🎉 agybot Deployment Completed Successfully!${RESET}"
echo -e "${BOLD}${GREEN}==============================================================================${RESET}"
echo -e "
${BOLD}Quick Reference:${RESET}
  • ${BOLD}Project directory:${RESET}   ${SCRIPT_DIR}
  • ${BOLD}Tmux session:${RESET}        ${TMUX_SESSION_NAME}
  • ${BOLD}Antigravity command:${RESET} ${AGY_COMMAND}
  • ${BOLD}Restart service:${RESET}     ./scripts/restart.sh (or 'sudo systemctl restart agybot')
  • ${BOLD}View live logs:${RESET}      journalctl -u agybot -f (or cat agybot.log)

${BOLD}How to use in Telegram:${RESET}
  1. Open your Telegram supergroup forum.
  2. Create a new topic (or navigate to an existing topic).
  3. Send any message in the topic:
     ${DIM}agybot will display the interactive Window & Directory Picker.${RESET}
  4. Select your project folder:
     ${DIM}A new tmux window will launch running Antigravity in accept-edits mode!${RESET}
  5. Chat freely — all responses, tool executions, and artifacts stream to Telegram.
"
