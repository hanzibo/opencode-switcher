# OpenCode Switcher — Agent Instructions

Linux desktop tray app (GTK3) that switches between OpenCode (CLI) sessions via a Spotlight-like search panel.

## Developer Commands

- **Run (dev):** `venv/bin/python3 main.py` (requires `opencode` CLI in PATH)
- **Run (prod):** `./run.sh` (logs to `run.log` with 10MB rotation; loads nvm for `opencode` CLI; uses `venv/bin/python3`)
- **Venv Setup:** `python3 -m venv --system-site-packages venv && venv/bin/pip install pynput>=1.7 python-xlib>=0.33` (requires system packages `python3-gi`, `python3-pip`, `python3-venv` from apt first)
- **Install (system-wide):** `./install.sh install` (copies files to `~/.local/share/opencode-switcher/`, enables systemd user service)
- **Uninstall:** `./install.sh uninstall`
- **Status Check:** `./install.sh status`
- **Testing/CI:** No automated test framework, linter, formatter, typechecker, CI, or remote repository exists. Do not attempt to run pytest or push changes.

## Architecture

- `main.py`: App entry point. Wires components, manages the Ayatana AppIndicator3 system tray menu, acquires the single-instance lock (`~/.config/opencode-switcher/lock`), and handles restart relaunch.
- `panel.py`: GTK3 search panel with tab switcher (Sessions / Clipboard). Implements focus out guards.
- `clipboard_panel.py`: Three-column clipboard view (Categories | Items | Action Buttons). Custom categories behave like prompts with inline CRUD. Employs `Gio.FileMonitor` to watch for updates.
- `clipboard_store.py`: Clipboard data layer. Saves history in `clipboard_history.json` (FIFO 150) and user categories in `categories.json`. Saves images as PNG files in `images/<hash>.png` to keep JSON lightweight.
- `session_store.py`: Interacts with OpenCode SQLite DB (`~/.local/share/opencode/opencode.db`). Scans `/proc` command lines to detect active sessions.
- `hotkey.py`: Standardizes X11 global hotkey via `pynput` and Wayland socket hotkey via local socket Unix server.
- `launcher.py`: Automatically discovers available terminals (Ptyxis → GNOME Terminal → Console → Black Box) and executes OpenCode sessions.
- `gnome-extension/`: GNOME Shell extension (`clipboard-monitor@opencode-switcher`) for Wayland. Records text copies natively and flags updates to the Python backend via marker files.

**Data Paths:**
- Configuration & History: `~/.config/opencode-switcher/` (`clipboard_history.json`, `categories.json`, `images/`)
- Runtime Socket & Markers: `~/.cache/opencode-switcher/` (`toggle.sock`, `clipboard.updated`, `last_written_hash`, `focus.request`)

## Platform Dual-Mode (X11 vs Wayland)

- **X11**: Python daemon thread runs `_clipboard_loop` to poll clipboard every 3s via `xclip`. Global hotkey `Ctrl+Shift+Space` handled by `pynput`. Window activation handled via `xdotool`.
- **Wayland**: Background Python polling is disabled. Clipboard updates are event-driven via the GNOME Shell extension. Global hotkey requires a custom GNOME shortcut bound to `opencode-switcher-toggle` (which sends `b"toggle"` to the Unix socket). Window activation uses `focus.request` file watched by GNOME Shell.

## Clipboard Update Protocol

Communication between GNOME extension and Python app uses marker files:
1. **Copy Detection**:
   - **Text**: GNOME Extension captures text via `St.Clipboard`, appends to `clipboard_history.json`, writes `text:<timestamp>` to `clipboard.updated`.
   - **Image**: GNOME Extension writes `image:<timestamp>` to `clipboard.updated`. Python app detects marker and pulls image via `wl-paste` command.
2. **Update UI**: `ClipboardPanel` watches `clipboard.updated` via `Gio.FileMonitor` and reloads cached data.
3. **Prevent Recapture loops**: When copying from the history panel back into the system clipboard, the item's hash is written to `last_written_hash`. Both extension and Python poll skip capturing items matching this hash.

## SQLite Database Coupling

- Reads OpenCode database: `~/.local/share/opencode/opencode.db` (tables `session` and `part`).
- To avoid concurrent write deadlocks with OpenCode, connection must use `timeout=5` and `PRAGMA journal_mode=WAL`.
- Exclude archived sessions (`time_archived IS NULL`), subagent sessions (`title NOT LIKE '%(@%subagent)%'`), and directories that no longer exist.

## Critical Developer Conventions

- **GTK Thread Safety**: The GTK loop is pure synchronous. UI updates from non-main threads must be wrapped in `GLib.idle_add(callback, *args)`. Do not use `asyncio`.
- **Wayland Focus Flashing**: To prevent Xwayland focus synchronization from causing system Dock flashing, always use `load_cached()` (read JSON directly) instead of `load_data()` (calls xclip/wl-paste) on Wayland when displaying the panel.
- **Focus Guards**: The panel hides on focus-out. Set `_menu_active`, `_delete_in_progress`, or `_dialog_active` flags to `True` during menus/dialogs to prevent the panel from hiding prematurely.
- **Tab Switching Anti-Flicker**: Wrap tab-switch text/placeholder changes with `handler_block()` and `handler_unblock()` on the search entry's `"search-changed"` signal to avoid redundant redraws.
- **Compatibility**: Use `typing` module type annotations (`Tuple`, `Dict`, `List`, `Optional`) for backward compatibility instead of Python 3.9+ lowercase generics (`tuple`, `dict`, `list`).
