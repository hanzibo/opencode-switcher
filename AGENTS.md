# OpenCode Switcher — Agent Instructions

Linux GTK3 desktop tray app that switches between OpenCode (CLI) sessions via a search panel.

## Developer Commands

- **Run (dev):** `venv/bin/python3 main.py` (requires `opencode` CLI in PATH)
- **Run (prod):** `./run.sh` (logs to `run.log` with 10MB rotation; loads nvm; uses `venv/bin/python3`)
- **Venv Setup:** `python3 -m venv --system-site-packages venv && venv/bin/pip install -r requirements.txt` (requires apt system packages: `gir1.2-ayatanaappindicator3-0.1 python3-gi python3-pip python3-venv wl-clipboard xclip xdotool`)
- **Install (system-wide):** `./install.sh install` (copies files to `~/.local/share/opencode-switcher/`, enables systemd user service, installs GNOME extension)
- **Uninstall / Status:** `./install.sh uninstall` | `./install.sh status`
- **Testing:** No CI, linter, typechecker (mypy/pyright), or formatter exists. Test drag-and-drop interactively via standalone script: `venv/bin/python3 dnd_test.py`
- **DB inspection:** `venv/bin/python3 inspect_db.py` — prints OpenCode SQLite table schemas and recent sessions

## Architecture & Data Paths

- `main.py`: Entrypoint, tray menu, single-instance lock (`~/.config/opencode-switcher/lock`), restart relaunch.
- `panel.py`: GTK3 search panel with tab switcher.
- `clipboard_panel.py` & `clipboard_store.py`: Clipboard UI and store. Saves history in `clipboard_history.json` (FIFO 150) and custom categories in `categories.json`. Images are stored under `images/<hash>.png`.
- `session_store.py`: Reads OpenCode SQLite DB (`~/.local/share/opencode/opencode.db`). Detects live sessions via `pgrep -f opencode` with `/proc` fallback.
- `hotkey.py`: X11 global hotkey (via `pynput`) and Wayland socket listener (Unix socket `~/.cache/opencode-switcher/toggle.sock`).
- `launcher.py`: Terminal discovery (Ptyxis → GNOME Terminal → Console/kgx → Black Box) and session spawner.
- `gnome-extension/`: GNOME Shell extension for clipboard capture and window activation on Wayland.
- `opencode-switcher-toggle`: Thin shell script sending `"toggle"` to the Unix socket — bind this to your GNOME keyboard shortcut.
- `codebase_analysis.md`: Comprehensive Chinese-language architecture deep read (~160 lines).

## Platform Dual-Mode (X11 vs Wayland)

- **X11**: Background thread polls clipboard via `xclip` every 3s. Global hotkey `Ctrl+Shift+Space` handled by `pynput`. Window activation via `xdotool`.
- **Wayland**: Background poll disabled. Clipboard updates captured via GNOME extension (writes to `clipboard_history.json` and signals updates via `~/.cache/opencode-switcher/clipboard.updated`). Global hotkey requires a custom GNOME shortcut bound to `opencode-switcher-toggle`. Window activation uses `focus.request` file watched by GNOME extension.

## SQLite Database Coupling

- Read from `~/.local/share/opencode/opencode.db`. Connection must use `timeout=5` and `PRAGMA journal_mode=WAL` to avoid write deadlocks with OpenCode.
- Exclude archived sessions (`time_archived IS NOT NULL`), subagent sessions (`title LIKE '%(@%subagent)%'`), and directories that no longer exist.

## Critical GTK & PyGObject Quirks (Crash Guards)

- **Thread Safety**: GTK is synchronous. All UI updates from background threads use `GLib.idle_add(callback, *args)`. No `asyncio`.
- **Signal Callback Mutation Safety**: Never modify the widget tree hierarchy (destroy, rebuild, show popup menus) synchronously inside a GTK event callback (`button-press-event`, selection changes). This destroys the C-level signal source and triggers `SIGSEGV`. Always defer via `GLib.idle_add()`.
- **Focus-Active Widget Safety**: Never destroy or remove a focused widget (e.g., inline edit `Entry`). Shift focus away with `window.set_focus(None)` first.
- **Dialog Destruction Trap**: Capture dialog properties (`dialog.get_filename()`) *before* calling `dialog.destroy()` — calling `destroy()` first returns `None`.
- **Global CSS Provider Leak**: Do not use `add_provider_for_screen` for local styles — it leaks globally. Use `widget.get_style_context().add_provider(...)` instead.
- **Nested Dialog Focus Guard**: The `_dialog_active` flag prevents panel hide on focus-out. Only manage via outermost dialog's `show`/`destroy` signals. Inner nested dialogs must not trigger `on_dialog_hidden()` or clear this flag.
- **GTK CSS Limits**: GTK 3 CSS has no `!important`. Use higher specificity (e.g., `dialog headerbar.titlebar`). Clear default gradients with `background-image: none;` and `box-shadow: none;` on custom dark-themed nodes.
- **Wayland Focus Flashing**: Prevent Xwayland dock flashing: use `load_cached()` (reads JSON cache) instead of `load_data()` (calls xclip/wl-paste) on Wayland when rendering the panel.
- **Anti-Flicker**: Wrap tab-switch placeholder changes with `handler_block()`/`handler_unblock()` on the search entry's `search-changed` signal.
- **Single Instance Restart Lock Order**: Release the `flock` lock fd *before* calling `subprocess.Popen`. Spawning before release makes the new instance fail lock acquisition.
- **Compatibility**: Use `typing` annotations (`Tuple`, `Dict`, `List`, `Optional`) instead of Python 3.9+ lowercase generics for backward compatibility.

## Systemd Service

- `opencode-switcher.service`: `Restart=on-failure`, `RestartSec=3`, `KillMode=process`. Runs after `graphical-session.target`.
