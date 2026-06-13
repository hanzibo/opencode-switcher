# OpenCode Switcher — Agent Instructions

Linux GTK3 desktop tray app that switches between OpenCode (CLI) sessions via a search panel.

## Developer Commands

- **Run (dev):** `venv/bin/python3 main.py` (requires `opencode` CLI in PATH)
- **Run (prod):** `./run.sh` (logs to `run.log` with 10MB rotation; loads nvm; uses `venv/bin/python3`)
- **Venv Setup:** `python3 -m venv --system-site-packages venv && venv/bin/pip install -r requirements.txt` (requires apt system packages: `gir1.2-ayatanaappindicator3-0.1 python3-gi python3-pip python3-venv wl-clipboard xclip xdotool`)
- **Install (system-wide):** `./install.sh install` (copies files to `~/.local/share/opencode-switcher/`, enables systemd user service, installs GNOME extension)
- **Uninstall / Status:** `./install.sh uninstall` | `./install.sh status`
- **Testing:** No CI/linter exists. Test drag-and-drop interactively via standalone script: `venv/bin/python3 dnd_test.py`

## Architecture & Data Paths
- `main.py`: Entrypoint, tray menu, single-instance lock (`~/.config/opencode-switcher/lock`), restart relaunch.
- `panel.py`: GTK3 search panel with tab switcher.
- `clipboard_panel.py` & `clipboard_store.py`: Clipboard UI and store. Saves history in `clipboard_history.json` (FIFO 150) and custom categories in `categories.json`. Images are stored under `images/<hash>.png`.
- `session_store.py`: Reads OpenCode SQLite DB (`~/.local/share/opencode/opencode.db`).
- `hotkey.py`: X11 global hotkey (via `pynput`) and Wayland socket hotkey (Unix socket `~/.cache/opencode-switcher/toggle.sock`).
- `launcher.py`: Terminal discovery (Ptyxis → GNOME Terminal → Console → Black Box) and session spawner.
- `gnome-extension/`: GNOME Shell extension for clipboard capture and window activation on Wayland.

## Platform Dual-Mode (X11 vs Wayland)

- **X11**: Background thread polls clipboard via `xclip` every 3s. Global hotkey `Ctrl+Shift+Space` handled by `pynput`. Window activation via `xdotool`.
- **Wayland**: Background poll disabled. Clipboard updates captured via GNOME extension (writes to `clipboard_history.json` and signals updates via `~/.cache/opencode-switcher/clipboard.updated`). Global hotkey requires a custom GNOME shortcut bound to `opencode-switcher-toggle`. Window activation uses `focus.request` file watched by GNOME extension.

## SQLite Database Coupling
- Read from `~/.local/share/opencode/opencode.db`. Connection must use `timeout=5` and `PRAGMA journal_mode=WAL` to avoid write deadlocks with OpenCode.
- Exclude archived sessions (`time_archived IS NOT NULL`), subagent sessions (`title LIKE '%(@%subagent)%'`), and directories that no longer exist.

## Critical GTK & PyGObject Quirks (Crash Guards)
- **Thread Safety**: GTK is synchronous. All UI updates from background threads must be wrapped in `GLib.idle_add(callback, *args)`. Do not use `asyncio`.
- **Signal Callback Mutation Safety**: Never modify the widget tree hierarchy (e.g., destroying, rebuilding widgets, or showing popup menus) synchronously inside a GTK event callback (e.g., `button-press-event` or selection changes). This destroys the C-level signal source and triggers a `SIGSEGV` segmentation fault. Always defer via `GLib.idle_add()`.
- **Focus-Active Widget Safety**: Never destroy or remove a widget while it holds focus (e.g., inline edit `Entry`). Shift the window focus away first using `window.set_focus(None)` to prevent C-level GTK crashes.
- **Dialog Destruction Trap**: When using GTK dialogs (e.g., `Gtk.FileChooserDialog`), capture any required properties (like `dialog.get_filename()`) *before* calling `dialog.destroy()`. Calling `destroy()` first will return `None`.
- **Global CSS Provider Leak**: Do not use `add_provider_for_screen` for local window styles (like sorting or recycle bin dialogs). It leaks memory globally. Register styles directly on the widget context: `widget.get_style_context().add_provider(...)`.
- **Nested Dialog Focus Guard**: The focus protector flag (`_dialog_active`) prevents the main panel from hiding on focus-out. Only manage this flag via the outermost dialog's `show`/`destroy` signals. Inner nested confirmation dialogs must *not* trigger `on_dialog_hidden()` or clear this flag.
- **GTK CSS Limits**: The GTK 3 CSS engine does not support `!important`. Increase selector specificity instead (e.g., `dialog headerbar.titlebar`). Clear default gradients using `background-image: none;` and `box-shadow: none;` on custom dark-themed nodes.
- **Wayland Focus Flashing**: To prevent Xwayland focus sync dock flashing, always use `load_cached()` (reads JSON cache) instead of `load_data()` (calls xclip/wl-paste) on Wayland when rendering the panel.
- **Anti-Flicker**: Wrap tab-switch text/placeholder changes with `handler_block()` and `handler_unblock()` on the search entry's `search-changed` signal to avoid redundant redraws.
- **Single Instance Restart Lock Order**: Release the `flock` lock file descriptor *before* calling `subprocess.Popen` to spawn the new process. If spawned before release, the new instance will fail lock acquisition and exit immediately.
- **Compatibility**: Use `typing` module type annotations (`Tuple`, `Dict`, `List`, `Optional`) for backward compatibility instead of Python 3.9+ lowercase generics (`tuple`, `dict`, `list`).
