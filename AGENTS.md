# OpenCode Switcher — Agent Instructions

Linux desktop tray app that switches between OpenCode (CLI) sessions via a Spotlight-like search panel.

## Developer Commands

- **Run (dev):** `venv/bin/python3 main.py` (venv must exist; `opencode` CLI in PATH)
- **Run (prod):** `./run.sh` — logs to `run.log` with 10MB rotation; loads nvm for `opencode` CLI; uses `venv/bin/python3`
- **Venb setup:** `python3 -m venv --system-site-packages venv && venv/bin/pip install pynput>=1.7 python-xlib>=0.33` — requires `python3-gi`, `python3-pip`, `python3-venv` from apt first
- **Install system-wide:** `./install.sh install` (copies files to `~/.local/share/opencode-switcher/`, enables systemd user service)
- **Uninstall:** `./install.sh uninstall`
- **Status:** `./install.sh status`
- **No test framework, linter, formatter, typechecker, CI, or remote** exists. `git push` will fail — no remote configured.

## Architecture

| File | Role |
|------|------|
| `main.py` | Entry point. `App` class: single-instance lock (`fcntl.flock`), tray indicator (Ayatana AppIndicator3), wires hotkey ↔ panel ↔ launcher ↔ clipboard |
| `panel.py` | GTK3 panel (~1176 lines). Tab bar (Sessions / Clipboard) + `Gtk.Stack`. Sessions: two-pane directory sidebar + session list with fuzzy search. |
| `clipboard_panel.py` | Three-column Clipboard/Prompts view: category sidebar, content list, action button bar. Gio file monitor watches `clipboard.updated` marker file for clipboard changes. |
| `clipboard_store.py` | Data layer: `ClipboardStore` (FIFO 150, hash-dedup, image support), `PromptStore` (CRUD), `capture_clipboard_once()` polls `wl-paste`/`xclip`. |
| `session_store.py` | Reads OpenCode's SQLite DB (`~/.local/share/opencode/opencode.db`). `/proc` live-session detection (excludes self). Soft-delete and rename via SQL UPDATE. |
| `hotkey.py` | X11: `pynput` global hotkey `Ctrl+Shift+Space`. Wayland: Unix socket at `~/.cache/opencode-switcher/toggle.sock` triggered by `opencode-switcher-toggle` script. |
| `launcher.py` | Auto-detects terminal (ptyxis → gnome-terminal → kgx → blackbox). Wayland: skips `xdotool` window activation. Terminal WM_CLASS mapped dynamically (e.g., `ptyxis|org.gnome.Ptyxis`). |
| `utils.py` | Shared `is_wayland()` detection and `relative_time()` converter (extracted from panel/clipboard_panel to deduplicate). |
| `gnome-extension/extension.js` | GNOME Shell extension for Wayland clipboard monitoring. Listens to `owner-changed` on `global.display.get_selection()`. Writes text directly to JSON history, signals image via marker. Also handles window-focus requests via file monitor. |
| `gnome-extension/metadata.json` | Extension UUID: `clipboard-monitor@opencode-switcher`. Targets GNOME Shell 48–50. |
| `inspect_db.py` | Standalone debug script that dumps OpenCode's SQLite schema and recent sessions. Useful when upstream schema changes break `session_store.py`. |
| `codebase_analysis.md` | Detailed Chinese-language analysis doc (~155 lines) covering architecture, historical bugs, and critical flow logic. Read this for deep understanding. |

**Data files:** `~/.config/opencode-switcher/clipboard_history.json` (FIFO 150, includes image metadata), `prompts.json`, `images/<hash>.png`

## Platform & Display Server Dual-Mode

- **X11**: `pynput` global hotkey, `xdotool` window activation, 3-second timer polling clipboard via `xclip`.
- **Wayland**: Unix socket hotkey listener (triggered by `opencode-switcher-toggle` script), GNOME Shell extension for clipboard monitoring, NO window activation (skipped). No polling timer — the extension's `owner-changed` signal drives updates.
- **Session detection**: `/proc` scanning and `fcntl.flock` are Linux-specific. Self-exclusion: skips processes where `cmdline` contains both `b"python3"` and `b"opencode-switcher"`.

## Clipboard Update Protocol (Critical)

Communication between the GNOME Shell extension and Python app uses marker files in `~/.cache/opencode-switcher/`:

1. **Extension** (Wayland only) detects clipboard change via `owner-changed` signal:
   - Text: reads via `St.Clipboard.get_text()`, appends directly to `clipboard_history.json`, writes `text:<timestamp>` to `clipboard.updated`
   - Image: writes `image:<timestamp>` to `clipboard.updated` (Python side fetches the image bytes via `wl-paste`)
2. **ClipboardPanel** watches `clipboard.updated` via `Gio.FileMonitor` and reloads when it changes
3. **`_last_written_hash`** mechanism: When the app itself writes to clipboard (e.g., user copies from history), the hash is saved to `last_written_hash` file. Both extension and Python poll skip items matching this hash to prevent re-capture.
4. **Focus request** (Wayland): The panel writes a window class name to `~/.cache/opencode-switcher/focus.request`; the GNOME extension monitors this file and calls `win.activate()` on matching windows.

## Session Store — SQLite Coupling

- Reads `~/.local/share/opencode/opencode.db`, tables `session` and `part`
- Uses `PRAGMA journal_mode=WAL` and `timeout=5` to avoid deadlocks with OpenCode's own SQLite writes
- Filters out archived sessions (`time_archived IS NULL`), subagent sessions (`title NOT LIKE '%(@%subagent)%'`), and sessions whose directory no longer exists
- If OpenCode's schema changes upstream, `session_store.py` breaks — verify column names when debugging

## Conventions

- Pure synchronous GTK event loop (`Gtk.main()`); no `asyncio`
- Error returns as `Optional[str]` (None = success, string = error message)
- UI callbacks from non-main threads use `GLib.idle_add()`
- `gc.collect()` called on panel hide
- Panel focus guards: `_menu_active` and `_delete_in_progress` flags suppress `hide()` on focus-out during menus/dialogs
- Tab-switch anti-flicker: `handler_block(_search_changed_id)` while updating search text/placeholder, then `handler_unblock()` — prevents redundant redraws
- Wayland panel loads: `load_cached()` instead of `load_data()` to avoid Xwayland focus sync
- `typing` module (`Tuple`, `Dict`) used instead of Python 3.9+ lowercase generics for compatibility
- `requirements.txt` lists PyGObject but it comes from apt (`python3-gi`); pip-installed deps are only `pynput>=1.7` and `python-xlib>=0.33`

## Key Dependencies

- **Python (apt):** `python3-gi`, `python3-pip`, `python3-venv`
- **Python (venv pip):** `pynput>=1.7`, `python-xlib>=0.33`
- **System:** `gir1.2-ayatanaappindicator3-0.1`, `wl-clipboard` (Wayland) / `xclip` (X11)
- **Runtime:** `opencode` CLI (npm); `run.sh` loads nvm so it's in PATH

## Service & Toggle

- `opencode-switcher.service` uses `KillMode=process` — stopping the app does NOT kill launched terminal sessions
- `opencode-switcher-toggle` script (in `~/.local/bin/` after install) sends a `b"toggle"` packet to the Unix socket
- On Wayland, users must bind this script to `Ctrl+Shift+Space` in GNOME keyboard shortcuts (the pynput listener only works on X11)

## UI Interaction

- **Ctrl+Shift+Space** — toggle panel
- **Ctrl+1 / Ctrl+2** — switch tab (Sessions / Clipboard)
- **Sessions tab**: Up/Down/Enter navigate, Ctrl+R rename, Delete archive, Tab focus cycle (search → dir sidebar → list)
- **Clipboard tab**: Up/Down/Enter navigate, Delete remove item, search entry filters visible items; images shown as 40px-tall thumbnails via `GdkPixbuf`
- **Right-click** — context menu (rename/archive/start-pure for sessions; copy/delete for clipboard; edit/delete for prompts)
- **Escape** — close panel
- Special session IDs: `new-opencode` (launch new), `open-folder` (file picker), `gemini-query`/`google-query` (slash commands) — these cannot be deleted/renamed
