# OpenCode Switcher — Agent Instructions

Linux GTK3 desktop tray app switching between OpenCode (CLI) sessions via a search panel.
Python 3 + GTK3 + AyatanaAppIndicator. No CI/linter/formatter/typechecker. No automated tests. Manual testing only (`dnd_test.py`, 15s auto-exit).

## STRUCTURE

```
./                          # Flat project root (no __init__.py — not a package)
├── main.py                 # Entrypoint: flock lock, App(), Gtk.main()
├── panel.py                # Search panel UI (~1350 lines), tab switcher, slash commands, CSS providers, evdev keyboard injection
├── clipboard_panel.py      # Clipboard/LLM panel — largest file (~7100 lines)
├── clipboard_store.py      # Clipboard store (~1000 lines), heuristic classification, categories, prompts, LLM config, conversations
├── tool_registry.py        # AI tool definitions + executors — 8 tools, ReAct dispatcher (~1070 lines)
├── session_store.py        # SQLite reader + live-session detection via pgrep/proc
├── hotkey.py               # X11 pynput + Wayland Unix socket hotkey manager
├── launcher.py             # Terminal discovery + session spawner
├── utils.py                # is_wayland(), relative_time(), request_window_focus(), cache dirs
├── migrate_history.py      # Migration utility (dual-use: standalone + imported by main.py)
├── inspect_db.py           # DB inspector — missing `__name__` guard (top-level SQL on import)
├── dnd_test.py             # Only test file: interactive GTK DnD test (manual, 15s auto-exit)
├── codebase_analysis.md    # Stale architecture overview (file sizes predate large clipboard_panel growth)
├── katex/                  # KaTeX rendering files (CSS/JS/fonts for math in AI WebView)
├── gnome-extension/        # GNOME Shell extension (Wayland clipboard + focus)
│   ├── extension.js        # Clipboard owner-changed listener + focus request
│   └── metadata.json       # Shell versions [48,49,50]
├── docs/usage.md           # Chinese-language usage guide
├── run.sh                  # Prod launcher: log rotation, nvm, exec to main.py
├── install.sh              # Install/uninstall/status: systemd, venv, GNOME ext
├── requirements.txt        # PyGObject, pynput, python-xlib, markdown, pygments, requests
├── opencode-switcher-toggle # Shell→Python hybrid: sends "toggle"/"toggle_ai" to Unix socket
├── opencode-switcher.desktop # Desktop entry (placeholder __INSTALL_DIR__ replaced at install time)
├── opencode-switcher.service # systemd unit: Restart=on-failure, RestartSec=3, KillMode=process
├── LICENSE                 # MIT
└── opencode-switcher.png   # Tray icon
```

### Tribal Knowledge Directories

| Path | Contents |
|------|----------|
| `.hzb-agents/experience/` | 72 experience files — per-feature postmortems with pitfalls, solutions, and reasoning |
| `.omo/plans/` | 32 structured work plans from past feature development |
| `.omo/evidence/` | 2 verification artifacts from past sessions |

## COMMANDS

| Action | Command | Notes |
|--------|---------|-------|
| Run (dev) | `venv/bin/python3 main.py` | Needs `opencode` in PATH |
| Run (prod) | `./run.sh` | 10MB log rotation, nvm, exec to main.py |
| Venv setup | `python3 -m venv --system-site-packages venv && venv/bin/pip install -r requirements.txt` | `--system-site-packages` is required for system PyGObject |
| Install | `./install.sh install` | Copies to `~/.local/share/opencode-switcher/`, enables systemd, installs GNOME ext |
| Uninstall | `./install.sh uninstall` | Interactive — asks about keeping user data |
| Status | `./install.sh status` | Checks install dir, desktop entry, service, opencode CLI, GNOME ext |
| Test | `venv/bin/python3 dnd_test.py` | Manual GTK DnD test (15s auto-exit) |
| DB inspect | `venv/bin/python3 inspect_db.py` | Lists session table schema + latest rows |

**System deps** (beyond pip): `gir1.2-ayatanaappindicator3-0.1 python3-gi python3-pip python3-venv wl-clipboard xclip xdotool gir1.2-webkit2-4.1` (webkit2gtk for AI panel WebView)

**Commit convention** (observed from git log): `fix(area):`, `feat(area):`, `improve(area):`, `refactor(area):`, `style(area):`, `merge:`. Area prefix follows module (e.g., `ai-panel`, `theme`, `tool-registry`).

## KEY FEATURES NOT OBVIOUS FROM FILENAMES

### Dual Hotkey System
- **`Ctrl+Shift+Space`**: Toggle main panel (session search / clipboard)
- **`Ctrl+Shift+X`**: Toggle AI panel directly (opens clipboard tab + AI sidebar)
- On X11: handled by `pynput` GlobalHotKeys
- On Wayland: GNOME Shell shortcut → `opencode-switcher-toggle [--ai]` → Unix socket → `toggle.sock`
- Socket messages: `b"toggle"` or `b"toggle_ai"`

### Slash Commands (in session search bar)
| Command | Action |
|---------|--------|
| `/new [directory]` | Start new OpenCode session |
| `/open` | File chooser to pick directory → new session |
| `/gm <query>` | Open Gemini, copy query to clipboard, auto-type via keyboard injection |
| `/google <query>` | Open Google AI search (`udm=50`), auto-type via keyboard injection |

- Tab-completion supported for all slash commands
- `/gm` auto-typing: `evdev.UInput` (hardware-level) → `pynput` (fallback)
- `/gm` delay: 1.2s if Firefox already running, 4.0s if not

### Clipboard Panel (Tab 2)
- 5 filter tabs: All / Text / Image / Link / Code
- Code items show language tag (Python, Shell, JavaScript, C++, SQL, etc.) — uppercase
- Language detection uses heuristic scoring (regex-based) — duplicated in Python (`clipboard_store.py`) and JS (`gnome-extension/extension.js`)
- Language detection result cached back to item (`item.language`)
- Image clipboard: saves as PNG to `~/.config/opencode-switcher/images/<hash>.png`
- Dedup: by SHA-256 hash (16-char prefix), skip if `last_written_hash` matches, skip if same as last item
- Max 150 items, FIFO eviction
- Sensitive MIME type guard: skips `x-kde-passwordManagerHint`

### Custom Categories System
- Sidebar with pinned/unpinned categories, separator between them
- Built-in "Clipboard" category (`__clipboard__`) is immutable
- Drag-and-drop reordering of items within categories
- Per-category sort dialog (DnD with visual hover feedback)
- Recycle bin with restore to original category (by ID or name fallback)
- Backup/restore: JSON export/import of all categories + recycle bin

### Template/Dynamic Copy System
- Prompts support `${index:prompt=default}` placeholders
- `${&}` embeds clipboard content at that position
- Escape `\${&}` renders as literal `${&}`
- Prompts Config dialog has `+ ${&}` quick-insert button
- `TEMPLATE_REGEX` and `PROMPT_PLACEHOLDER_RE` in `clipboard_panel.py`

### AI Assistant Panel (WebKit2 WebView)
- Multi-turn conversation with LLM (via OpenAI-compatible API)
- Markdown rendering in WebView with code highlighting (via `markdown` + `pygments` + CodeHilite)
- Configuration: `~/.config/opencode-switcher/llm_settings.json` (saved with `0o600` permissions — contains API keys)
- Multi-model support with alias, base_url, api_key, model_name
- Conversation history stored as individual JSON files in `~/.cache/opencode-switcher/conversations/`
- `ConversationStore` manages per-conversation persistence
- `CustomPromptsStore` manages named prompts with categories + action_type (web, ai_chat)
- Default prompt: "Ask Google" (Chinese: "以上内容是什么意思，如果是代码，请分析并注释。")
- WebKit settings optimized: WebGL/HTML5 databases/localStorage disabled to reduce memory
- **ReAct Tool Calling loop**: LLM can call 8 tools (web_search, web_fetch, list_directory, read_file, get_current_time, grep_search, glob_find, file_info). See "AI Tool Calling" section below.

### AI Tool Calling (ReAct Loop)
Integrated across `clipboard_panel.py` (ReAct loop, `_ToolCallAccumulator`) and `tool_registry.py` (tool definitions + executors):

- **Loop**: LLM streams response → if `finish_reason: "tool_calls"`, accumulate SSE deltas via `_ToolCallAccumulator` → execute synchronously via `tool_registry.execute_tool_call()` → feed result back as `role: "tool"` message → repeat.
- **Safety limit**: `MAX_TOOL_ITERATIONS = 25` (kills runaway loops)
- **8 tools registered** in `tool_registry.py`:
  - `web_search` — Obscura headless browser search
  - `web_fetch` — Obscura page fetch
  - `list_directory` — directory listing (safe-path guarded)
  - `read_file` — read file contents (safe-path guarded, 8192-byte binary pre-check)
  - `get_current_time` — timezone-aware current time
  - `grep_search` — recursive regex search, brace-expansion includes, binary skip
  - `glob_find` — recursive pattern match via `pathlib.Path.rglob`
  - `file_info` — stat metadata, symlink-safe detection
- **Tool results** rendered as collapsible HTML sections in the WebView
- **`ChatMessage`** in `clipboard_store.py` extended with `tool_call_id` and `tool_calls` fields for tool round-trip persistence
- **`TOOL_CHOICE_AUTO`** configurable per request in `clipboard_panel.py`
- **Tool definitions schema**: OpenAI function-calling format (`TOOL_DEFINITIONS` in `tool_registry.py`)

## TOOL_REGISTRY DETAILS

`tool_registry.py` (~1070 lines) is a self-contained module housing all tool schema definitions, executors, and display helpers:

- **`TOOL_DEFINITIONS`**: `List[Dict]` — OpenAI function-calling schemas for all 8 tools
- **`TOOL_EXECUTORS`**: `Dict[str, Callable]` — maps tool name → executor function
- **`execute_tool_call(tool_call: dict) -> str`**: dispatches by function name, truncates at `MAX_TOOL_RESULT_CHARS = 5000`
- **`_resolve_safe_path(path) -> Optional[str]`**: safety guard used by all file tools — validates abs path, resolves realpath, rejects non-existent paths
- **Helper functions**: `_glob_match()` (brace expansion for `{a,b}` patterns), `_format_file_size()` (human-readable byte sizes)
- **Display helpers**: `format_tool_calls_for_display()`, `format_tool_result_for_display()`, `render_collapsible_tool_result()`
- **Limits**: `_MAX_GREP_RESULTS = 200`, `_MAX_LINES_PER_FILE = 50`, `_MAX_GLOB_RESULTS = 500`
- **File safety**: 3-layer guard (null check → `os.path.isabs` → `os.path.realpath`), binary file detection (8192-byte null byte scan)

## PLATFORM DUAL-MODE

| Aspect | X11 | Wayland |
|--------|-----|---------|
| Clipboard capture | Background thread polls `xclip` every 3s | GNOME extension listens `owner-changed` signal → writes `clipboard_history.json` + `clipboard.updated` marker |
| Hotkey | `pynput` GlobalHotKeys | GNOME Shell shortcut → `opencode-switcher-toggle` → Unix socket |
| Window focus | `xdotool` | `utils.request_window_focus()` writes to `focus.request` file → GNOME extension monitors → `win.activate()` |
| Clipboard UI panel | `load_data()` → `capture_clipboard_once()` then `reload()` | `load_cached()` only (no polling) |

GNOME extension (`clipboard-monitor@opencode-switcher`):
- Duplicates clipboard classification logic (Python `classify_text` ≈ JS `classifyText`)
- Monitors `focus.request` file for window activation requests
- Checks `last_written_hash` to skip app's own clipboard writes
- Image clipboard: writes `clipboard.updated` marker with `image:` prefix

## STARTUP FLOW

```
systemd/.desktop → run.sh → main.py (flock lock)
  → _load_config() → migrate_history.run_migration()
  → ClipboardStore → CategoryStore → SearchPanel+ClipboardPanel → HotkeyManager
  → App.run(): hotkey start → clipboard thread start (X11 only) → Gtk.main()
  → Ctrl+C: app.stop() → flock release
  → Restart: close lock fd BEFORE subprocess.Popen(self)
```

## SQLITE DATABASE COUPLING

- **DB**: `~/.local/share/opencode/opencode.db`
- **Connection**: `timeout=5`, `PRAGMA journal_mode=WAL` (prevents deadlock with OpenCode)
- **Exclude**: archived sessions (`time_archived IS NOT NULL`), subagent sessions (`title LIKE '%(@%subagent)%'`), dirs that no longer exist
- **Snippet extraction**: Reads latest `part` row per session, extracts text from JSON `data` field (supports `type=text|reasoning|tool`)
- **Live detection**: `pgrep -f opencode` scan `/proc/<pid>/cmdline` and `/proc/<pid>/cwd`. Filters out the switcher itself (`opencode-switcher` in cmdline). Also checks `--session` flag in cmdline.
- **Status**: "live" (currently running), "recent" (<24h), "closed"

## CONFIG & CACHE PATHS

| Path | Contents |
|------|----------|
| `~/.config/opencode-switcher/config.json` | Theme setting |
| `~/.config/opencode-switcher/clipboard_history.json` | Clipboard items (150 FIFO) |
| `~/.config/opencode-switcher/categories.json` | Custom categories + recycle bin |
| `~/.config/opencode-switcher/custom_prompts.json` | Named prompts |
| `~/.config/opencode-switcher/llm_settings.json` | LLM API keys (perms 0o600) |
| `~/.config/opencode-switcher/lock` | Flock lock file |
| `~/.config/opencode-switcher/images/` | Clipboard image PNGs |
| `~/.cache/opencode-switcher/toggle.sock` | Unix socket for Wayland hotkey |
| `~/.cache/opencode-switcher/conversations/` | AI conversation JSON files |
| `~/.cache/opencode-switcher/clipboard.updated` | Marker file (Wayland) |
| `~/.cache/opencode-switcher/last_written_hash` | Hash of last app-written content |
| `~/.cache/opencode-switcher/focus.request` | Focus request (Wayland) |

## CONVENTIONS

- **Strings**: double quotes (2250:187 ratio vs single). Docstrings: `"""`
- **Imports**: stdlib → third-party → local, `gi.require_version()` before `from gi.repository import ...`
- **Types**: `from typing import Tuple, Dict, List, Optional` — NOT Python 3.9+ lowercase generics (backward compat)
- **Thread safety**: `GLib.idle_add(callback, *args)` for any background→UI update. No `asyncio`.
- **Platform check**: `utils.is_wayland()` (reads `XDG_SESSION_TYPE` / `WAYLAND_DISPLAY`)
- **Naming**: PascalCase classes, snake_case functions, `_prefix` for private, `UPPER_CASE` for constants
- **Comments**: `# <space><text>`, Chinese or English. Use `# ponytail:` for intentionally removed code references.
- **Entry points**: `if __name__ == "__main__":` guard required (current: `inspect_db.py` missing this)
- **No linter/formatter/CI**: Manual discipline required. No `asyncio`. No `assert` for tests (manual only).

## ANTI-PATTERNS (THIS PROJECT)

- **No package structure**: Zero `__init__.py` files. All modules flat in root.
- **No automated tests**: Zero. `dnd_test.py` is manual-only (15s auto-exit). No `pytest`.
- **No CI/CD**: No GitHub Actions, Makefile, Dockerfile. `install.sh` is Debian/Ubuntu-only.
- **`add_provider_for_screen` used in both panels** (panel.py, clipboard_panel.py) — leaks CSS globally per GTK docs (functional tradeoff accepted).
- **`inspect_db.py` missing `__name__` guard**: Top-level SQL executes on import (currently safe, not imported, but fragile).
- **`opencode-switcher-toggle`**: Python code inside shell script via `exec python3 -c "..."` — fragile quoting, no linting.
- **`run.sh` sources NVM**: Couples tray app runtime to user's shell Node.js env.
- **`--system-site-packages` venv**: Breaks isolation. Required workaround for PyGObject being a system package.
- **Hardcoded version** (`VERSION="1.0.0"` in `install.sh`) — no git tags, no version automation.
- **WebKit2 dependency** (`gir1.2-webkit2-4.1`) is NOT in `install.sh` system deps but required at runtime (AI panel crashes without it).
- **GNOME extension duplicates Python classification logic** in JavaScript — two codebases to keep in sync.

## CRITICAL GTK & PYGObject QUIRKS (Crash Guards)

- **Signal Callback Safety**: Never modify widget tree hierarchy inside GTK event callbacks (destroy, rebuild, popup menus). Destroys C-level signal source → SIGSEGV. Defer via `GLib.idle_add()`.
- **Focus-Active Widget Safety**: Never destroy/remove a focused `Entry`. Call `window.set_focus(None)` first.
- **Dialog Destruction Trap**: Read `dialog.get_filename()` *before* `dialog.destroy()` (destroy returns None).
- **CSS Provider Scope**: Use `widget.get_style_context().add_provider(...)`. Never `add_provider_for_screen` (global leak) — though the codebase does it anyway.
- **Nested Dialog Focus Guard**: `_dialog_active` flag — managed via `on_dialog_shown`/`on_dialog_hidden` callbacks spanning both SearchPanel and ClipboardPanel. Inner dialogs must NOT trigger `on_dialog_hidden()` or clear this flag.
- **GTK3 CSS Limits**: No `!important`. Use higher specificity. Clear default gradients via `background-image: none; box-shadow: none;`.
- **Wayland Focus Flashing**: Use `load_cached()` (JSON cache) not `load_data()` (xclip/wl-paste) on Wayland.
- **Anti-Flicker**: Wrap tab-switch placeholder changes with `handler_block()`/`handler_unblock()` on `search-changed`.
- **Restart Lock Order**: Release `flock` lock fd *before* `subprocess.Popen`. Spawning before release makes new instance fail lock acquisition.
- **Signal Loop Storms (UI Freezing)**: ListBox rows removal/addition inside selection or change callbacks triggers recursive selection/changed signals. Use `handler_block`/`handler_unblock` during rebuilding, check `row.get_parent() == listbox` in selection callbacks, prefer in-place label updates over widget tree rebuilding.
- **Window Stretching Prevention**: `Gtk.ListBox` content expansion can override fixed-height windows (`set_resizable(False)`). Always wrap variable-length list containers in `Gtk.ScrolledWindow` with policies `(NEVER, AUTOMATIC)`.
- **Swallowed/Silenced Exceptions**: PyGObject callbacks swallow Python tracebacks. Inspect `~/.local/share/opencode-switcher/run.log` for NameErrors/syntax errors.

## GNOME EXTENSION NOTES

- UUID: `clipboard-monitor@opencode-switcher`
- Shell versions: 48, 49, 50
- `owner-changed` signal on `global.display.get_selection()` (type 1 = CLIPBOARD)
- Classifies clipboard content in JS (duplicated from Python `clipboard_store.py`)
- Writes directly to `clipboard_history.json` (shares file with Python side — race condition possible)
- `focus.request` file monitor watches for window focus requests
- Own writes tracked via `isInternalWrite` flag to avoid echo on `focus.request`

## SYSTEMD SERVICE

`opencode-switcher.service`: `Restart=on-failure`, `RestartSec=3`, `KillMode=process`. Runs after `graphical-session.target`.
