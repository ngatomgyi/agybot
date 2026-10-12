#!/usr/bin/env bash
# restart.sh — Restart agybot service or background process

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

echo "🔄 Restarting agybot..."

# Check if systemd unit is active
if systemctl is-active --quiet agybot 2>/dev/null; then
    echo "Restarting agybot systemd service..."
    sudo systemctl restart agybot
    echo "✅ Service restarted via systemd."
    systemctl status agybot --no-pager
    exit 0
fi

# Fallback: kill existing agybot process and restart with uv in tmux or background
echo "Restarting agybot process..."
pkill -f "agybot" || true
sleep 1

cd "${PROJECT_DIR}"
if [ -f ".venv/bin/agybot" ]; then
    nohup "${PROJECT_DIR}/.venv/bin/agybot" > "${PROJECT_DIR}/agybot.log" 2>&1 &
    echo "✅ agybot restarted in background (PID: $!). Logs: agybot.log"
else
    nohup uv run agybot > "${PROJECT_DIR}/agybot.log" 2>&1 &
    echo "✅ agybot restarted via uv run in background (PID: $!). Logs: agybot.log"
fi
