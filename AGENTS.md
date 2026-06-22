# OpenCode Switcher — Agent Instructions

Linux GTK3 desktop tray app switching between OpenCode (CLI) sessions via a search panel. Python 3 + GTK3 + AyatanaAppIndicator. No CI/linter/formatter/typechecker.

## STRUCTURE

```
./                          # Flat project root (no __init__.py — not a package)
├── main.py                 # Entrypoint: flock lock, App(), Gtk.main()
├── panel.py                # Search panel UI, tab switcher, CSS providers
├── clipboard_panel.py      # Clipboard/LLM panel — largest file
├── clipboard_store.py      # Clipboard store, heuristic classification, custom prompts
├── session_store.py        # SQLite reader + live-session detection via pgrep/proc
├── hotkey.py               # X11 pynput + Wayland Unix socket hotkey manager
├── launcher.py             # Terminal discovery + session spawner
├── utils.py                # is_wayland(), relative_time(), request_window_focus()
├── migrate_history.py      # Migration utility (dual-use: standalone + imported by main.py)
├── inspect_db.py           # DB inspector (missing __name__ guard)
├── dnd_test.py             # Only test file: interactive GTK DnD test (manual)
├── gnome-extension/        # GNOME Shell extension (Wayland clipboard + focus)
│   ├── extension.js        # Clipboard owner-changed listener + focus request
│   └── metadata.json       # Shell versions [48,49,50]
├── docs/usage.md           # Chinese-language usage guide
├── run.sh                  # Prod launcher: log rotation, nvm, exec to main.py
├── install.sh              # Install/uninstall/status: systemd, venv, GNOME ext
└── opencode-switcher-toggle # Shell→Python hybrid: sends "toggle" to Unix socket
```

## WHERE TO LOOK

| Task | File | Notes |
|------|------|-------|
| Session list / SQLite | `session_store.py` | WAL pragma, exclude archived+subagent |
| Clipboard capture+classify | `clipboard_store.py` | Heuristic classify, FIFO 150, image store |
| Clipboard UI | `clipboard_panel.py` | Tabbed panel, prompts, backup/restore |
| Search panel | `panel.py` | Fuzzy scoring, tab switcher, CSS themes |
| Hotkey / socket | `hotkey.py` | pynput (X11) vs Unix socket (Wayland) |
| Terminal launch | `launcher.py` | Ptyxis→GNOME Terminal→Console→Black Box |
| GNOME extension | `gnome-extension/extension.js` | Wayland clipboard monitor + focus |
| Config + cache paths | `utils.py` | `~/.config/...` and `~/.cache/...` |

## COMMANDS

- **Run (dev):** `venv/bin/python3 main.py` (needs `opencode` in PATH)
- **Run (prod):** `./run.sh` (10MB log rotation, nvm, exec to main.py)
- **Venv setup:** `python3 -m venv --system-site-packages venv && venv/bin/pip install -r requirements.txt` (system deps: `gir1.2-ayatanaappindicator3-0.1 python3-gi python3-pip python3-venv wl-clipboard xclip xdotool`)
- **Install:** `./install.sh install` (copies to `~/.local/share/opencode-switcher/`, enables systemd, installs GNOME extension)
- **Uninstall / Status:** `./install.sh uninstall` | `./install.sh status`
- **Test:** `venv/bin/python3 dnd_test.py` (manual GTK DnD test)
- **DB inspect:** `venv/bin/python3 inspect_db.py`

## STARTUP FLOW

```
systemd/.desktop → run.sh → main.py (flock lock)
  → App.__init__(): sync migration → ClipboardStore → SearchPanel+ClipboardPanel → HotkeyManager
  → App.run(): hotkey thread → clipboard poll thread (X11 only) → Gtk.main()
  → Ctrl+C: app.stop() → flock release
  → Restart: release flock BEFORE Popen(self)
```

## PLATFORM DUAL-MODE (X11 vs Wayland)

- **X11**: Poll clipboard via `xclip` every 3s (background thread). Global hotkey `Ctrl+Shift+Space` via `pynput`. Window activation via `xdotool`.
- **Wayland**: No clipboard polling. GNOME extension captures clipboard changes (writes `clipboard_history.json`, signals `clipboard.updated`). Hotkey via GNOME shortcut → `opencode-switcher-toggle`. Focus via `focus.request` file watched by extension.

## SQLITE DATABASE COUPLING

- **DB**: `~/.local/share/opencode/opencode.db`. Connection: `timeout=5`, `PRAGMA journal_mode=WAL` (prevents deadlock with OpenCode).
- **Exclude**: archived sessions (`time_archived IS NOT NULL`), subagent sessions (`title LIKE '%(@%subagent)%'`), dirs that no longer exist.

## CONVENTIONS

- **Strings**: double quotes (3243:194 ratio vs single). Docstrings: `"""`
- **Imports**: stdlib → third-party → local, `gi.require_version()` before `from gi.repository import ...`
- **Types**: `from typing import Tuple, Dict, List, Optional` — NOT Python 3.9+ lowercase generics (backward compat)
- **Thread safety**: `GLib.idle_add(callback, *args)` for any background→UI update. No `asyncio`.
- **Platform check**: `utils.is_wayland()` (reads `XDG_SESSION_TYPE` / `WAYLAND_DISPLAY`)
- **Config/cache paths**: `~/.config/opencode-switcher/` (JSON configs), `~/.cache/opencode-switcher/` (socket, images, clipboard history)
- **Naming**: PascalCase classes, snake_case functions, `_prefix` for private, `UPPER_CASE` for constants
- **Comments**: `# <space><text>`, Chinese or English. Use `# ponytail:` for intentionally removed code references.
- **Entry points**: `if __name__ == "__main__":` guard required (current: `inspect_db.py` missing this)
- **No linter/formatter/CI**: Manual discipline required. No `asyncio`. No `assert` for tests (manual only).

## ANTI-PATTERNS (THIS PROJECT)

- **No package structure**: Zero `__init__.py` files. Can't `pip install -e .` or use `python -m`. All modules flat in root.
- **No tests**: Zero automated tests. `dnd_test.py` is manual-only. No `pytest`, no assertions anywhere.
- **No CI/CD**: No GitHub Actions, no Makefile, no Dockerfile. `install.sh` is Debian/Ubuntu-only (hardcoded `dpkg`).
- **`add_provider_for_screen` used despite being forbidden** (panel.py and clipboard_panel.py — leaks CSS globally per GTK docs).
- **`inspect_db.py` missing `__name__` guard**: Top-level SQL executes on import (currently safe, not imported).
- **`opencode-switcher-toggle`**: Python code inside shell script via `exec python3 -c "..."` — fragile quoting, no linting.
- **`run.sh` sources NVM**: Couples tray app runtime to user's shell Node.js env. NVM errors pollute app log.
- **`--system-site-packages` venv**: Breaks isolation. Workaround for PyGObject being a system package.
- **Hardcoded version** `VERSION="1.0.0"` in `install.sh` — no git tags, no version automation.

## CRITICAL GTK & PYGObject QUIRKS (Crash Guards)

- **Signal Callback Safety**: Never modify widget tree hierarchy inside GTK event callbacks (destroy, rebuild, popup menus). Destroys C-level signal source → SIGSEGV. Defer via `GLib.idle_add()`.
- **Focus-Active Widget Safety**: Never destroy/remove a focused `Entry`. Call `window.set_focus(None)` first.
- **Dialog Destruction Trap**: Read `dialog.get_filename()` *before* `dialog.destroy()` (destroy returns None).
- **CSS Provider Scope**: Use `widget.get_style_context().add_provider(...)`. Never `add_provider_for_screen` (global leak).
- **Nested Dialog Focus Guard**: `_dialog_active` flag — manage only via outermost dialog's `show`/`destroy` signals. Inner dialogs must NOT trigger `on_dialog_hidden()` or clear this flag.
- **GTK3 CSS Limits**: No `!important`. Use higher specificity. Clear default gradients via `background-image: none; box-shadow: none;`.
- **Wayland Focus Flashing**: Use `load_cached()` (JSON cache) not `load_data()` (xclip/wl-paste) on Wayland.
- **Anti-Flicker**: Wrap tab-switch placeholder changes with `handler_block()`/`handler_unblock()` on `search-changed`.
- **Restart Lock Order**: Release `flock` lock fd *before* `subprocess.Popen`. Spawning before release makes new instance fail lock acquisition.
- **Signal Loop Storms (UI Freezing)**: ListBox rows removal/addition inside selection or change callbacks will trigger recursive selection/changed signals. Use `listbox.handler_block(handler_id)` and `handler_unblock(handler_id)` during rebuilding, check `row.get_parent() == listbox` in row selection callbacks, and prefer in-place label updates over widget tree rebuilding.
- **Window Stretching Prevention**: `Gtk.ListBox` content expansion can override fixed-height windows (`set_resizable(False)`). Always wrap variable-length list containers in `Gtk.ScrolledWindow` with policies `(NEVER, AUTOMATIC)`.
- **Swallowed/Silenced Exceptions**: PyGObject callbacks swallow general Python tracebacks, rendering UI buttons silently unresponsive. Always inspect `~/.local/share/opencode-switcher/run.log` to check for NameErrors or syntax errors.

## SYSTEMD SERVICE

`opencode-switcher.service`: `Restart=on-failure`, `RestartSec=3`, `KillMode=process`. Runs after `graphical-session.target`.
