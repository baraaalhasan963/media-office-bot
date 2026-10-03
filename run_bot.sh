#!/usr/bin/env bash
# ==============================================================================
# Media Office Bot - Server Runner / Watchdog
# Suitable for Linux VPS, PythonAnywhere, or Background Tasks
# ==============================================================================

set -e

# Change to the bot repository root directory
DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null 2>&1 && pwd )"
cd "$DIR"

# Activate Virtual Environment if present
if [ -d "venv" ]; then
    echo "Activating virtualenv (venv)..."
    source venv/bin/activate
elif [ -d ".venv" ]; then
    echo "Activating virtualenv (.venv)..."
    source .venv/bin/activate
fi

echo "Starting Media Office Bot in auto-restart loop..."

while true; do
    python3 main.py
    EXIT_CODE=$?
    if [ $EXIT_CODE -eq 0 ]; then
        echo "Bot process exited cleanly (code 0). Stopping."
        break
    else
        echo "Bot process exited with code $EXIT_CODE. Restarting in 5 seconds..."
        sleep 5
    fi
done
