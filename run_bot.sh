#!/usr/bin/env bash
# ==============================================================================
# Media Office Bot - Server Runner / Watchdog
# Suitable for Linux VPS, PythonAnywhere, or Background Tasks
# ==============================================================================

# Change to the bot repository root directory
DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" >/dev/null 2>&1 && pwd )"
cd "$DIR"

# Select Python binary directly from virtualenv to avoid system PATH conflicts
PYTHON_BIN="python3"
if [ -f "$DIR/venv/bin/python" ]; then
    PYTHON_BIN="$DIR/venv/bin/python"
    source "$DIR/venv/bin/activate" 2>/dev/null || true
elif [ -f "$DIR/venv/bin/python3" ]; then
    PYTHON_BIN="$DIR/venv/bin/python3"
    source "$DIR/venv/bin/activate" 2>/dev/null || true
elif [ -f "$DIR/.venv/bin/python" ]; then
    PYTHON_BIN="$DIR/.venv/bin/python"
    source "$DIR/.venv/bin/activate" 2>/dev/null || true
fi

echo "Using Python binary: $PYTHON_BIN"
echo "Starting Media Office Bot in auto-restart loop..."

while true; do
    "$PYTHON_BIN" main.py
    EXIT_CODE=$?
    if [ $EXIT_CODE -eq 0 ]; then
        echo "Bot process exited cleanly (code 0). Stopping."
        break
    else
        echo "Bot process exited with code $EXIT_CODE. Restarting in 5 seconds..."
        sleep 5
    fi
done
