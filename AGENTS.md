# OpenCode Switcher — Agent Instructions

Linux GTK3 desktop tray app for switching between OpenCode (CLI) sessions, clipboard history management, and an AI assistant sidebar. Python 3 + GTK3 + AyatanaAppIndicator. **No CI/linter/formatter/typechecker. No automated tests.** ~461 commits, all by single author.

## Commands

| Action | Command | Notes |
|--------|---------|-------|
| Run (dev) | `venv/bin/python3 main.py` | Needs `opencode` in PATH |
| Run (prod) | `./run.sh` | 10MB log rotation, nvm, `JSC_useJIT=false` |
| Venv setup | `python3 -m venv --system-site-packages venv && venv/bin/pip install -r requirements.txt` | `--system-site-packages` required for system PyGObject |
| Install | `./install.sh install` | Copies to `~/.local/share/opencode-switcher/`, enables systemd, installs GNOME ext |
| Uninstall | `./install.sh uninstall` | Interactive — asks about keeping user data |
| Status | `./install.sh status` | Checks install dir, desktop entry, service, opencode CLI, GNOME ext |
| DB inspect | `venv/bin/python3 inspect_db.py` | Lists session table schema + latest rows |

**System deps** (beyond pip): `gir1.2-ayatanaappindicator3-0.1 python3-gi python3-pip python3-venv wl-clipboard xclip xdotool gir1.2-webkit2-4.1` — webkit2gtk is NOT in `install.sh` but required at runtime (AI panel crashes without it).

**Commit convention**: `fix(area):`, `feat(area):`, `improve(area):`, `refactor(area):`, `optimize(area):`, `perf(area):`, `style(area):`, `docs(area):`, `merge:`. Area prefix follows module (e.g., `ai-panel`, `theme`, `tool-registry`, `clipboard`).

## Architecture

### Entrypoint & Startup Flow

`main.py` (~281 lines) is the sole entrypoint. Startup sequence:
```
systemd/.desktop → run.sh → main.py (flock lock)
  → _load_config() → migrate_history.run_migration()
  → ClipboardStore → CategoryStore → SearchPanel+ClipboardPanel → HotkeyManager
  → App.run(): hotkey start → clipboard thread start (X11 only) → Gtk.main()
  → Ctrl+C: app.stop() → flock release
```
**Critical**: Release flock fd *before* `subprocess.Popen(self)` on restart — spawning before release makes new instance fail. `KillMode=process` in service file means systemd only kills the script process, not its children.

### Module Roles

| File | Lines | Role |
|------|-------|------|
| `main.py` | 281 | Entrypoint: flock lock, App(), Gtk.main() |
| `panel.py` | 1387 | Search panel — tab switcher, slash cmds, CSS-in-code, session list |
| `clipboard_panel.py` | 2189 | Clipboard panel — assembles subcomponents + event routing |
| `clipboard_store.py` | 1110 | God module — 12 classes: classification, clipboard storage, categories, conversations, LLM settings, prompts |
| `ai_chat_panel.py` | 2937 | AI assistant — WebView, LLM dialog, ReAct tool calls, round nav, lightbox, subagent status |
| `ai_tool_loop.py` | 238 | ReAct loop (max 25 iterations) |
| `ai_html_template.py` | 1197 | WebView HTML template + KaTeX + lightbox JS |
| `ai_popovers.py` | 522 | AI command autocomplete + conversation history popover |
| `ai_text_utils.py` | 917 | Pure markdown/math/vision helpers (zero GTK dep) |
| `llm_client.py` | 349 | LLM HTTP client + `_ToolCallAccumulator` for SSE delta merge |
| `session_store.py` | 202 | SQLite reader + live-session detection via `/proc` |
| `launcher.py` | 128 | Terminal auto-detection (Ptyxis → GNOME Terminal → Console/kgx → Black Box) + session spawner |
| `hotkey.py` | 87 | pynput (X11) + Unix socket (Wayland) |
| `settings_dialog.py` | 285 | Settings factory: AI truncation threshold, QQ Mail credentials |
| `tool_registry/` | 5000 | 25 tools across 12 modules |
| `gnome-extension/` | 350 | GNOME Shell extension (Wayland clipboard + focus IPC) |

**Flat root** — no `__init__.py`, not an importable Python package. All `.py` files imported directly by `main.py`.

### Tool Registry (`tool_registry/`)

25 AI tool executors dispatched via `TOOL_EXECUTORS` dict. Assembled from per-module `TOOL_SCHEMAS` lists in `__init__.py`. Key modules:
- `bash.py` — persistent bash session, sentinel protocol, interactive command blocking (hard blocks: `vi`, `less`, `top`, `ssh`; conditional: `ssh-keygen`, `openssl`, `gpg`)
- `web.py` — `web_search`/`web_fetch` with Obscura browser + trafilatura extraction, `cancel_event` support
- `filesystem.py` — safe-path guarded read/write/edit/delete/rename
- `code_analysis.py` — `get_code_metrics`, `find_project_dependencies`, `parse_file_ast` (1052 lines)
- `subagent.py` — `sub_agent`, `get_subagent_status` — parallel isolated execution
- `search.py` — `grep_search`, `glob_find`
- `todo.py` — persistent task management with dependency tracking

Tool calls: LLM streams → `_ToolCallAccumulator` merges SSE deltas → `execute_tool_call()` dispatches → result fed back as `role: "tool"`. Cancel via `cancel_event` threading.Event.

Each tool call now carries a `purpose` parameter (agent-generated description), displayed in the tool summary line alongside file/query/URL (via `_TOOL_DISPLAY_FIELD` mapping in `ai_text_utils.py` — 13 tools covered, module-level constant).

### Platform Dual-Mode

| Aspect | X11 | Wayland |
|--------|-----|---------|
| Clipboard capture | Background thread polls `xclip` every 3s | GNOME ext `owner-changed` → writes JSON + `clipboard.updated` marker |
| Hotkey | `pynput` GlobalHotKeys | GNOME Shell shortcut → `opencode-switcher-toggle` → Unix socket (`toggle.sock`) |
| Window focus | `xdotool` polling loop (20×150ms) | `utils.request_window_focus()` → `focus.request` file → GNOME ext monitors → `win.activate()` |
| Clipboard UI refresh | `load_data()` → `capture_clipboard_once()` → `reload()` | `load_cached()` only (no polling) |

**Wayland clipboard.updated marker**: Single-file timestamp overwritten by rapid clipboard events — not a queue. Lossy.

## Key Features & Quirks

### Hotkeys
- **Ctrl+Shift+Space**: Toggle main panel
- **Ctrl+Shift+X**: Toggle AI panel directly (opens clipboard tab + AI sidebar)
- Socket messages: `b"toggle"` or `b"toggle_ai"`

### Slash Commands (in search bar)
`/new`, `/open`, `/gm <query>`, `/google <query>`. Tab-completion. `/gm` uses `evdev.UInput` → `pynput` fallback. Delay: 1.2s (Firefox running) / 4.0s (not).

### Clipboard Classification
Heuristic regex scoring in `clipboard_store.py` (`classify_text()`, `detect_language_name()`). **Duplicated in `gnome-extension/extension.js`** — ~150 lines of scoring in both Python and JS. Must update both for any classification change.

### Template/Dynamic Copy
- `${&}` embeds clipboard content. `\${&}` → literal `${&}`.
- Multi‑parameter: `${index:prompt=default}`
- `TEMPLATE_REGEX` duplicated in `dynamic_copy_dialog.py`, `clipboard_panel.py`, `ai_chat_panel.py` — must keep in sync.

### AI Assistant (WebKit2 WebView)
- OpenAI-compatible API. Config: `~/.config/opencode-switcher/llm_settings.json` — **saved with `0o600`** (API keys).
- WebKit settings: WebGL/HTML5 DBs/localStorage disabled (memory).
- Conversation files: `~/.cache/opencode-switcher/conversations/` (JSON).
- `/cd <path>` command switches active bash cwd in AI panel.
- Subagent status bar: real-time status blocks with click-selection, adaptive polling lifecycle.
- Conversation HTML caching: history switching renders from cached HTML instead of re-rendering.
- Truncation threshold configurable via Settings UI (`soft_limit`/`trim_target` in `~/.config/opencode-switcher/ai_settings.json`).
- Tool calls display `purpose` description + file path in summary line via `_TOOL_DISPLAY_FIELD` + `_render_tool_step()`.

### AI Input: Multi-line Preservation
When re-rendering chat after AI responds, the original Shift+Enter line breaks in user messages are preserved via `_preserve_newlines()`. This function detects fenced code blocks to avoid adding `<br>` inside them.

### SQLite Database Coupling
- **DB**: `~/.local/share/opencode/opencode.db`. Connection: `timeout=5`, `PRAGMA journal_mode=WAL`.
- **Exclude**: archived sessions, subagent sessions (`title LIKE '%(@%subagent)%'`), non-existent dirs.
- **Live detection**: `pgrep -f opencode` → scan `/proc/<pid>/cmdline` + `/proc/<pid>/cwd`. Filters out switcher itself. Also checks `--session` flag.
- **Status**: "live" (running), "recent" (<24h), "closed".

## Config & Cache Paths

| Path | Contents |
|------|----------|
| `~/.config/opencode-switcher/config.json` | Theme setting (dark/light) |
| `~/.config/opencode-switcher/clipboard_history.json` | 150 FIFO clipboard items |
| `~/.config/opencode-switcher/categories.json` | Custom categories + recycle bin |
| `~/.config/opencode-switcher/custom_prompts.json` | Named prompts |
| `~/.config/opencode-switcher/llm_settings.json` | LLM API keys (perms 0o600) |
| `~/.config/opencode-switcher/ai_settings.json` | AI truncation threshold (`soft_limit`, `trim_target`) |
| `~/.config/opencode-switcher/lock` | Flock lock file |
| `~/.config/opencode-switcher/images/` | Clipboard image PNGs |
| `~/.cache/opencode-switcher/toggle.sock` | Unix socket (Wayland hotkey) |
| `~/.cache/opencode-switcher/conversations/` | AI conversation JSON files |
| `~/.cache/opencode-switcher/clipboard.updated` | Marker file (Wayland clipboard IPC) |
| `~/.cache/opencode-switcher/last_written_hash` | Hash of last app-written content |
| `~/.cache/opencode-switcher/focus.request` | Focus request (Wayland) |

## Conventions

- **Strings**: double quotes (~10:1 over single). Docstrings: `"""`
- **Imports**: stdlib → third-party → local. `gi.require_version()` BEFORE `from gi.repository import ...`
- **Thread safety**: `GLib.idle_add(callback, *args)` for background→UI updates. No `asyncio`.
- **Platform check**: `utils.is_wayland()` reads `XDG_SESSION_TYPE` / `WAYLAND_DISPLAY`
- **Comments**: `# <space><text>`, Chinese or English.
- **`# ponytail:`** marks intentionally removed code — searchable breadcrumb for deleted blocks.
- **`console.error('opencode-switcher: ...')`** prefix in GNOME extension JS error messages.
- **Settings dialog**: factory pattern `show_settings_dialog(parent, on_dialog_shown, on_dialog_hidden)`. Reuses focus-guard `_dialog_active` flag. Extensible via `_tabs` list.

## Anti-Patterns (This Project)

- **`add_provider_for_screen`** in 3 files (panel.py, clipboard_panel.py, ai_chat_panel.py) — leaks CSS globally (accepted tradeoff).
- **`opencode-switcher-toggle`**: Python code inside shell script via `exec python3 -c "..."` — fragile quoting.
- **`run.sh` sources NVM**: Couples tray app runtime to user's shell Node.js env.
- **`--system-site-packages` venv**: Breaks isolation (required for system PyGObject).
- **Hardcoded version** (`VERSION="1.0.0"` in `install.sh`) — no git tags or version automation.
- **WebKit2 dependency** not in `install.sh` but required at runtime — AI panel crashes without it.
- **GNOME extension duplicates Python classification** — must keep both in sync manually.
- **Shared `clipboard_history.json`** — written by both Python and JS, no locking → potential corruption.
- **`TEMPLATE_REGEX` duplicated** in 3 files — must keep in sync.
- **Lossy marker IPC** (`clipboard.updated`): single-file timestamp, not a queue.

## Critical GTK & PyGObject Crash Guards

- **Signal callback safety**: Never modify widget tree hierarchy inside GTK event callbacks — destroys C-level signal source → SIGSEGV. Defer via `GLib.idle_add()`.
- **Focused widget safety**: Never destroy/remove a focused `Entry`. Call `window.set_focus(None)` first.
- **Dialog destruction trap**: Read `dialog.get_filename()` *before* `dialog.destroy()` — destroy returns None.
- **CSS Provider scope**: Use `widget.get_style_context().add_provider(...)`. `add_provider_for_screen` leaks globally.
- **Nested dialog focus guard**: `_dialog_active` flag via `on_dialog_shown`/`on_dialog_hidden`. Inner dialogs must NOT trigger `on_dialog_hidden()`.
- **GTK3 CSS limits**: No `!important`. Higher specificity required. Clear default gradients via `background-image: none; box-shadow: none;`.
- **Wayland focus flashing**: Use `load_cached()` (JSON cache), not `load_data()` (xclip/wl-paste).
- **Anti-flicker**: Wrap tab-switch placeholder changes with `handler_block()`/`handler_unblock()` on `search-changed`.
- **Signal loop storms**: ListBox row removal/addition inside selection callbacks triggers recursion. Use `handler_block`/`handler_unblock`, check `row.get_parent() == listbox`, prefer in-place label updates.
- **Swallowed exceptions**: PyGObject callbacks swallow tracebacks. Check `run.log` for NameErrors/syntax errors.

## Reference

- `.hzb-agents/experience/` — ~108 per-feature postmortems (pitfalls, solutions, reasoning)
- `.omo/plans/` — 41 structured work plans from past development
- `gnome-extension/` — GNOME Shell extension for Wayland clipboard + focus IPC. See `gnome-extension/AGENTS.md`.
