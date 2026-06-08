# Project Instructions

OpenCode Switcher — Linux desktop tray app that switches between OpenCode (CLI) sessions via a Spotlight-like search panel.

## Developer Commands

- **Run (dev):** `venv/bin/python3 main.py` (repo root; `opencode` CLI must be in PATH; requires venv due to PEP 668)
- **Run (prod):** `./run.sh` — logs to `run.log` with 10MB rotation; loads nvm for `opencode` CLI; uses `venv/bin/python3`
- **Install:** `./install.sh install`
- **Uninstall:** `./install.sh uninstall`
- **Status:** `./install.sh status`

No test framework, linter, formatter, or typechecker exists.

## Architecture

| File | Role |
|------|------|
| `main.py` | Entry point. `App` class: single-instance lock (`fcntl.flock`), tray indicator (Ayatana AppIndicator3), wires hotkey ↔ panel ↔ launcher ↔ clipboard |
| `panel.py` | GTK3 panel (~960 lines). Tab bar (Sessions / Clipboard) + `Gtk.Stack`. Sessions: two-pane directory sidebar + session list with fuzzy search. |
| `clipboard_panel.py` | Three-column Clipboard/Prompts view: category sidebar, content list, action button bar. Gio file monitor watches `clipboard.updated` marker file for clipboard changes. |
| `clipboard_store.py` | Data layer: `ClipboardStore` (FIFO 150, hash-dedup), `PromptStore` (CRUD), `capture_clipboard_once()` polls `wl-paste`/`xclip`. |
| `hotkey.py` | X11: `pynput` global hotkey `Ctrl+Shift+Space`. Wayland: Unix socket at `~/.cache/opencode-switcher/toggle.sock` triggered by `opencode-switcher-toggle` script. |
| `session_store.py` | Reads OpenCode's SQLite DB at `~/.local/share/opencode/opencode.db`. `/proc` live-session detection (excludes self). Soft-delete and rename via SQL UPDATE. |
| `launcher.py` | Auto-detects terminal (ptyxis → gnome-terminal → kgx → blackbox). Wayland: skips `xdotool` window activation. |

**Data files:** `~/.config/opencode-switcher/clipboard_history.json` (FIFO 150), `prompts.json`

## Key Dependencies

- **Python (apt):** `python3-gi`, `python3-pip`, `python3-venv`
- **Python (venv pip):** `pynput>=1.7`, `python-xlib>=0.33`
- **System:** `gir1.2-ayatanaappindicator3-0.1`, `wl-clipboard` (Wayland) / `xclip` (X11)
- **Runtime:** `opencode` CLI (npm); `run.sh` loads nvm so it's in PATH

## Service & Toggle

- `opencode-switcher.service` uses `KillMode=process` so stopping the app does not kill launched terminal sessions.
- `opencode-switcher-toggle` script sends a "toggle" signal to the running instance's Unix socket. Bind this to `Ctrl+Shift+Space` in GNOME keyboard shortcuts on Wayland.

## Platform Constraints

- **X11/Wayland dual support**: `pynput`/`xdotool`/`GdkX11` only work on X11. Wayland uses Unix socket hotkey listener and skips window activation.
- `/proc` scanning and `fcntl.flock` are Linux-specific.
- `session_store.py` queries OpenCode's internal SQLite schema (`session`, `part` tables) — schema changes upstream break this.

## Conventions

- Pure synchronous GTK event loop (`Gtk.main()`); no `asyncio`
- Error returns as `Optional[str]` (None = success, string = error message)
- UI callbacks from non-main threads use `GLib.idle_add()`
- `gc.collect()` called on panel hide
- Panel focus guards: `_menu_active` and `_delete_in_progress` flags suppress `hide()` on focus-out during menus/dialogs

## UI Interaction

- **Ctrl+Shift+Space** — toggle panel
- **Ctrl+1 / Ctrl+2** — switch tab (Sessions / Clipboard)
- **Sessions tab**: Up/Down/Enter navigate, Ctrl+R rename, Delete archive, Tab focus cycle (search → dir sidebar → list)
- **Clipboard tab**: Up/Down/Enter navigate, Delete remove item, search entry filters visible items
- **Right-click** — context menu (rename/archive for sessions; copy/delete for clipboard; edit/delete for prompts)
- **Escape** — close panel
