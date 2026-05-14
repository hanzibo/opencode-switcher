# OpenCode Switcher

Flat Python/GTK3 system-tray app that searches OpenCode's SQLite database and launches sessions in `gnome-terminal`.

## Setup

```bash
# System deps (apt-get)
xdotool
gir1.2-ayatanaappindicator3-0.1

# Python deps
pip install -r requirements.txt

# `opencode` CLI must be on PATH (the run.sh wrapper adds it)
```

## Run

```bash
# Direct
python3 main.py

# With logging + Node in PATH
./run.sh
```

No test/lint/typecheck/build commands exist.

## Important details

- **Reads** `~/.local/share/opencode/opencode.db` read-only. Soft-delete writes `time_archived` on the `session` table.
- **Global hotkey** `Ctrl+Shift+Space` registered via `pynput`.
- **Launcher** hardcodes `gnome-terminal`. Session launch: `cd <dir> && exec opencode --session <id>`. Uses `xdotool` to activate the new window.
- **`.desktop` / `.service` files contain absolute paths** — update if repo moves.
- **`run.sh`** adds `opencode` to PATH via `nvm.sh`, and rotates `run.log` when >10 MB.
- **`.gitignore`** covers `__pycache__/`, `run.log`, and `*.pyc`.
- Session list filters out archived sessions and subagent sessions (`title NOT LIKE '%(@%subagent)%'`).
- Type `/new` in the search box to create a fresh OpenCode session.
- Status is "live" if an `opencode` process is running from the same directory and updated < 24h.
