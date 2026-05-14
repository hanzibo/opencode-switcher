#!/usr/bin/env bash
export PATH="$HOME/.nvm/versions/node/v22.22.1/bin:$PATH"
LOG="$HOME/workfiles/opencode-switcher/run.log"
echo "=== $(date) ===" >> "$LOG"
/usr/bin/python3 "$HOME/workfiles/opencode-switcher/main.py" >> "$LOG" 2>&1
