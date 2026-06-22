#!/usr/bin/env bash
SCRIPT_DIR="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
LOG="$SCRIPT_DIR/run.log"

# Rotate log if larger than 10 MB
if [ -f "$LOG" ] && [ "$(wc -c < "$LOG")" -gt 10485760 ]; then
    mv "$LOG" "$LOG.old"
fi

# Ensure opencode CLI is in PATH
# Try loading nvm if available (opencode is often installed via npm)
export NVM_DIR="$HOME/.nvm"
[ -s "$NVM_DIR/nvm.sh" ] && \. "$NVM_DIR/nvm.sh"

# Disable JIT for WebKit JavaScriptCore — reduces memory footprint
export JSC_useJIT=false

echo "=== $(date) ===" >> "$LOG"
exec "$SCRIPT_DIR/venv/bin/python3" "$SCRIPT_DIR/main.py" >> "$LOG" 2>&1
