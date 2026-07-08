# OpenCode Switcher — Agent Instructions

Linux GTK3 desktop tray app switching between OpenCode (CLI) sessions via a search panel.
Python 3 + GTK3 + AyatanaAppIndicator. No CI/linter/formatter/typechecker. No automated tests.

## LAYOUT

```
./                          # Flat root — no __init__.py, not a package
├── main.py                 # Entrypoint: flock lock, App(), Gtk.main() (~281 lines)
├── panel.py                # Search panel (~1382 lines) — tab switcher, slash cmds, CSS, evdev injection
├── clipboard_panel.py      # Clipboard panel container (~2097 lines) — assembles subcomponents + event routing
├── ai_chat_panel.py        # AI assistant sidebar (~2354 lines) — WebView, LLM dialog, ReAct tool calls
├── clipboard_store.py      # Store: classification, categories, prompts, LLM config, conversations (~1032 lines, 12 classes, 7 dataclasses)
├── tool_registry.py        # 24 AI tools, ReAct dispatcher, HTML formatting (~3249 lines, 79 functions)
├── session_store.py        # SQLite reader + live-session detection (~202 lines)
├── launcher.py             # Terminal auto-detection + session spawner (~128 lines)
├── hotkey.py               # pynput (X11) + Unix socket (Wayland) — only 87 lines
├── utils.py                # is_wayland(), relative_time(), request_window_focus(), dirs (~49 lines)
├── llm_client.py           # LLM HTTP client + _ToolCallAccumulator (~349 lines)
├── ai_tool_loop.py         # ReAct tool calling loop (~204 lines), MAX_TOOL_ITERATIONS=25
├── ai_html_template.py     # WebView HTML + KaTeX inline embedding + lightbox/zoom (~1183 lines)
├── ai_text_utils.py        # Pure markdown/math/vision helpers, zero GTK dep (~808 lines)
├── ai_popovers.py          # AI command autocomplete + history popovers (~522 lines)
├── prompts_config_dialog.py # Prompts/LLM-config dialog (~910 lines)
├── prompt_dialog.py        # Create/edit prompt dialog (~77 lines)
├── dynamic_copy_dialog.py  # Template placeholder dialog (~320 lines)
├── sort_dialog.py          # Sort items DnD dialog (~277 lines)
├── sort_cats_dialog.py     # Sort categories DnD dialog (~329 lines)
├── recycle_bin_dialog.py   # Recycle bin dialog (~240 lines)
├── migrate_history.py      # DB migration (~59 lines, dual-use: standalone + imported by main.py)
├── inspect_db.py           # DB inspector — __name__ guard present at line 10
├── opencode-switcher-toggle # Shell→Python hybrid: sends "toggle"/"toggle_ai" to Unix socket (14 lines)
├── katex/                  # KaTeX CSS/JS/fonts for math in AI WebView
├── gnome-extension/        # GNOME Shell extension (Wayland clipboard + focus IPC)
│   ├── extension.js        # ~350 lines — clipboard + focus + classification (duplicated from clipboard_store.py)
│   ├── metadata.json       # shell-version [48,49,50]
│   └── AGENTS.md           # GNOME extension-specific agent instructions
├── clipboard_store/        # Empty dir — possible package migration target
├── docs/usage.md           # Chinese-language usage guide
├── run.sh                  # Prod launcher: log rotation, nvm, JSC_useJIT=false
├── install.sh              # Install/uninstall/status: systemd, venv, GNOME ext (VERSION="1.0.0" hardcoded)
├── opencode-switcher.desktop
├── opencode-switcher.service # Restart=on-failure, RestartSec=3, KillMode=process
└── requirements.txt        # PyGObject, pynput, python-xlib, markdown, pygments, requests, trafilatura, pymdown-extensions
```

### Tribal Knowledge
| Path | Contents |
|------|----------|
| `.hzb-agents/experience/` | ~98 per-feature postmortems — pitfalls, solutions, reasoning |
| `.omo/plans/` | 38 structured work plans from past development |
| `.omo/evidence/` | 2 verification artifacts |

## COMMANDS

| Action | Command | Notes |
|--------|---------|-------|
| Run (dev) | `venv/bin/python3 main.py` | Needs `opencode` in PATH |
| Run (prod) | `./run.sh` | 10MB log rotation, nvm, JSC_useJIT=false |
| Venv setup | `python3 -m venv --system-site-packages venv && venv/bin/pip install -r requirements.txt` | `--system-site-packages` required for system PyGObject |
| Install | `./install.sh install` | Copies to `~/.local/share/opencode-switcher/`, enables systemd, installs GNOME ext |
| Uninstall | `./install.sh uninstall` | Interactive — asks about keeping user data |
| Status | `./install.sh status` | Checks install dir, desktop entry, service, opencode CLI, GNOME ext |
| DB inspect | `venv/bin/python3 inspect_db.py` | Lists session table schema + latest rows |

**System deps** (beyond pip): `gir1.2-ayatanaappindicator3-0.1 python3-gi python3-pip python3-venv wl-clipboard xclip xdotool gir1.2-webkit2-4.1` — webkit2gtk is NOT in install.sh but required at runtime (AI panel crashes without it).

**Commit convention**: `fix(area):`, `feat(area):`, `improve(area):`, `refactor(area):`, `optimize(area):`, `perf(area):`, `style(area):`, `docs(area):`, `merge:`. Area prefix follows module (e.g., `ai-panel`, `theme`, `tool-registry`, `clipboard`, `session-store`, `experience`). ~223 commits follow this pattern. Most cluster around `ai-panel` (~150) and `tool-registry` (~31).

## KEY FEATURES

### Dual Hotkey
- **Ctrl+Shift+Space**: Toggle main panel (session search / clipboard)
- **Ctrl+Shift+X**: Toggle AI panel directly (opens clipboard tab + AI sidebar)
- X11: `pynput` GlobalHotKeys. Wayland: GNOME Shell shortcut → `opencode-switcher-toggle [--ai]` → Unix socket → `toggle.sock`
- Socket messages: `b"toggle"` or `b"toggle_ai"`

### Slash Commands (in search bar)
- `/new [directory]` — start new session
- `/open` — file chooser → new session
- `/gm <query>` — Open Gemini + copy query + auto-type via keyboard injection
- `/google <query>` — Open Google AI search (`udm=50`) + auto-type
- Tab-completion supported. `/gm` uses `evdev.UInput` → `pynput` (fallback). Delay: 1.2s (Firefox running) / 4.0s (not).

### Clipboard Panel (Tab 2)
- 5 filter tabs: All / Text / Image / Link / Code
- Code items show language tag (uppercase: Python, Shell, JavaScript, C++, SQL...)
- Language detection: heuristic regex scoring — **duplicated** in Python (`clipboard_store.py`) and JS (`gnome-extension/extension.js`). Must update both.
- Image clipboard: PNG to `~/.config/opencode-switcher/images/<hash>.png`
- Dedup by SHA-256 (16-char prefix), skip if `last_written_hash` matches
- Max 150 items, FIFO eviction. Sensitive MIME guard: skips `x-kde-passwordManagerHint`

### Custom Categories
- Sidebar: pinned / unpinned (separator between), built-in `__clipboard__` (immutable)
- DnD reordering within categories, per-category sort dialog
- Recycle bin — restore to original category (by ID or name fallback)
- Backup/restore: JSON export/import

### Template/Dynamic Copy
- Prompts support `${index:prompt=default}` placeholders
- `${&}` embeds clipboard content at that position
- `\${&}` → literal `${&}` (escape via backslash)
- `TEMPLATE_REGEX` in `dynamic_copy_dialog.py`, `clipboard_panel.py`, and `ai_chat_panel.py` (duplicated — must keep in sync)

### AI Assistant (WebKit2 WebView)
- Multi-turn with LLM (OpenAI-compatible API). Markdown + code highlighting (pygments CodeHilite) + KaTeX math + `pymdown-extensions` (details, def_list)
- Config: `~/.config/opencode-switcher/llm_settings.json` — **saved with `0o600`** (contains API keys)
- Multi-model: alias, base_url, api_key, model_name
- Conversations: `~/.cache/opencode-switcher/conversations/` (JSON files)
- WebKit settings: WebGL/HTML5 DBs/localStorage disabled (memory optimization)
- `/cd <path>` command switches the AI panel's active bash working directory

### AI Tool Calling (ReAct Loop)
- **Architecture**: `llm_client.py` (`_LLMHttpClient`, `_ToolCallAccumulator` for SSE delta accumulation) → `ai_tool_loop.py` (`run_llm_react_loop` — max 25 iterations) → `tool_registry.py` (20 tool executors)
- LLM streams → if `finish_reason: "tool_calls"`, accumulate deltas → execute synchronously via `tool_registry.execute_tool_call()` → feed result back as `role: "tool"` → repeat
- **24 tools**: `web_search` / `web_fetch` (Obscura browser + trafilatura extraction), `list_directory` / `read_file` (supports line range) / `grep_search` / `glob_find` / `file_info` (safe-path guarded), `get_current_time`, `ask_user_question`, `write_file`, `edit_file` (exact-string replace with staleness check), `delete_file` / `rename_file` (file management), `todo_create` / `todo_update` / `todo_list` (persistent task management), `bash` (persistent bash session, supports `restart` and `timeout` params), `send_notification` (desktop notify-send), `sub_agent` (parallel isolated task execution), `get_subagent_status` (poll background sub-agent progress), `read_qq_mail` (IMAP QQ mailbox reader), `get_code_metrics` (code line count and structure analysis), `find_project_dependencies` (dependency graph and circular import detection), `parse_file_ast` (AST-based code structure analysis)
- Tool results rendered as collapsible `<pre>` sections in WebView
- `TOOL_CHOICE_AUTO` configurable per request

### AI Image Display
- Inline images in chat rendered as clickable thumbnails
- Lightbox overlay with zoom (mouse wheel), pan (click-drag), reset on double-click, close on Esc/click-outside
- Uses `requestAnimationFrame` for decoupling mousemove from browser frame ticks, disables transitions during drag

### AI Panel Navigation (Round Nav)
- Jump-to-top and jump-to-bottom buttons in round navigation bar
- Proximity-based round detection (not center-based) to prevent nav skip/flicker
- Throttled scroll handler via `requestAnimationFrame`

## PLATFORM DUAL-MODE

| Aspect | X11 | Wayland |
|--------|-----|---------|
| Clipboard capture | Background thread polls `xclip` every 3s | GNOME ext `owner-changed` → writes JSON + `clipboard.updated` marker |
| Hotkey | `pynput` GlobalHotKeys | GNOME Shell shortcut → `opencode-switcher-toggle` → Unix socket |
| Window focus | `xdotool` | `utils.request_window_focus()` → `focus.request` file → GNOME ext monitors → `win.activate()` |
| Clipboard UI | `load_data()` → `capture_clipboard_once()` → `reload()` | `load_cached()` only (no polling) |

GNOME extension mirrors Python classification logic in JS — must keep classified language tags in sync manually. Shared `clipboard_history.json` file has no locking → race condition on concurrent writes.

## STARTUP FLOW

```
systemd/.desktop → run.sh → main.py (flock lock)
  → _load_config() → migrate_history.run_migration()
  → ClipboardStore → CategoryStore → SearchPanel+ClipboardPanel → HotkeyManager
  → App.run(): hotkey start → clipboard thread start (X11 only) → Gtk.main()
  → Ctrl+C: app.stop() → flock release
  → Restart: close lock fd BEFORE subprocess.Popen(self) — spawning before release makes new instance fail
```

Note: `KillMode=process` in the service file means systemd only kills the script process, not its children. This is deliberate — restart must clean up properly.

## SQLITE DATABASE COUPLING

- **DB**: `~/.local/share/opencode/opencode.db`. Connection: `timeout=5`, `PRAGMA journal_mode=WAL`
- **Exclude**: archived sessions, subagent sessions (`title LIKE '%(@%subagent)%'`), non-existent dirs
- **Snippet extraction**: reads latest `part` row per session, JSON `data` field (`type=text|reasoning|tool`)
- **Live detection**: `pgrep -f opencode` → scan `/proc/<pid>/cmdline` + `/proc/<pid>/cwd`. Filters out switcher itself. Also checks `--session` flag
- **Status**: "live" (running), "recent" (<24h), "closed"

## CONFIG & CACHE PATHS

| Path | Contents |
|------|----------|
| `~/.config/opencode-switcher/config.json` | Theme setting (dark/light) |
| `~/.config/opencode-switcher/clipboard_history.json` | 150 FIFO clipboard items |
| `~/.config/opencode-switcher/categories.json` | Custom categories + recycle bin |
| `~/.config/opencode-switcher/custom_prompts.json` | Named prompts |
| `~/.config/opencode-switcher/llm_settings.json` | LLM API keys (perms 0o600) |
| `~/.config/opencode-switcher/lock` | Flock lock file |
| `~/.config/opencode-switcher/images/` | Clipboard image PNGs |
| `~/.cache/opencode-switcher/toggle.sock` | Unix socket (Wayland hotkey) |
| `~/.cache/opencode-switcher/conversations/` | AI conversation JSON files |
| `~/.cache/opencode-switcher/clipboard.updated` | Marker file (Wayland clipboard IPC) |
| `~/.cache/opencode-switcher/last_written_hash` | Hash of last app-written content |
| `~/.cache/opencode-switcher/focus.request` | Focus request (Wayland) |

## CONVENTIONS

- **Strings**: double quotes (~10220:1107 vs single chars). Docstrings: `"""`
- **Imports**: stdlib → third-party → local. `gi.require_version()` BEFORE `from gi.repository import ...`
- **Thread safety**: `GLib.idle_add(callback, *args)` for background→UI updates. No `asyncio`.
- **Platform check**: `utils.is_wayland()` reads `XDG_SESSION_TYPE` / `WAYLAND_DISPLAY`
- **Comments**: `# <space><text>`, Chinese or English.
- **`# ponytail:`** marks intentionally removed code — searchable breadcrumb for deleted blocks.
- **`console.error('opencode-switcher: ...')`** prefix in GNOME extension JS error messages.
- **X11 focus**: `xdotool windowactivate` with polling loop (20×150ms) in `launcher.py`.

## ANTI-PATTERNS (THIS PROJECT)

- **`add_provider_for_screen`** in both panels (`panel.py:101`, `clipboard_panel.py:203`) — leaks CSS globally per GTK docs (accepted tradeoff).
- **`opencode-switcher-toggle`**: Python code inside shell script via `exec python3 -c "..."` — fragile quoting.
- **`run.sh` sources NVM**: Couples tray app runtime to user's shell Node.js env.
- **`--system-site-packages` venv**: Breaks isolation. Required for system PyGObject.
- **Hardcoded version** (`VERSION="1.0.0"` in `install.sh`) — no git tags or version automation.
- **WebKit2 dependency** not in `install.sh` but required at runtime — AI panel crashes without it.
- **GNOME extension duplicates Python classification** — ~150 lines of heuristic scoring in both Python and JS.
- **Shared clipboard_history.json** — written by both Python and JS, no locking → potential corruption.
- **`TEMPLATE_REGEX` duplicated** in `dynamic_copy_dialog.py`, `clipboard_panel.py`, and `ai_chat_panel.py` — must keep in sync.
- **Lossy marker IPC** (`clipboard.updated`): single-file timestamp overwritten by rapid clipboard events — not a queue.

## COMPLEXITY HOTSPOTS

| File | Lines | Nature |
|------|-------|--------|
| `ai_chat_panel.py` | 2354 | AIChatPanel UI, WebView, LLM orchestration, stream+tool rendering, round nav, lightbox |
| `clipboard_panel.py` | 2097 | Large — was 7713 before extracting 11 modules + ai_chat_panel.py |
| `tool_registry.py` | 3249 | 20 tools, 3 classes + HTML formatting — verbose OpenAI JSON schemas account for size |
| `panel.py` | 1382 | Gatekeeper — 25+ event handlers. CSS-in-code (~140 lines template string). |
| `clipboard_store.py` | 1032 | God module — 12 classes (7 dataclasses, 5 stores): classification, clipboard storage, categories, conversation persistence, LLM settings, prompts. |
| `ai_html_template.py` | 1183 | WebView HTML template + KaTeX + lightbox JS + round nav CSS — grew significantly with visual features |
| `prompts_config_dialog.py` | 910 | 910-line single dialog class. |
| `gnome-extension/extension.js` | 350 | Compact but duplicates ~150 lines of classification logic. |

**Remaining refactoring candidates**: (1) `ClipboardPanel` still co-located with `clipboard_panel.py` (2097 lines) — worth extracting to own module. (2) Extract shared `classify_text()` module for Python and JS. (3) Prompts config dialog is standalone but 910 lines. (4) `clipboard_store/` dir exists empty — possible package migration target.

## CRITICAL GTK & PYGObject QUIRKS (Crash Guards)

- **Signal callback safety**: Never modify widget tree hierarchy inside GTK event callbacks (destroy, rebuild, popup menus) — destroys C-level signal source → SIGSEGV. Defer via `GLib.idle_add()`.
- **Focused widget safety**: Never destroy/remove a focused `Entry`. Call `window.set_focus(None)` first.
- **Dialog destruction trap**: Read `dialog.get_filename()` *before* `dialog.destroy()` — destroy returns None.
- **CSS Provider scope**: Use `widget.get_style_context().add_provider(...)`. `add_provider_for_screen` leaks globally (codebase uses it anyway).
- **Nested dialog focus guard**: `_dialog_active` flag managed via `on_dialog_shown`/`on_dialog_hidden` callbacks. Inner dialogs must NOT trigger `on_dialog_hidden()`.
- **GTK3 CSS limits**: No `!important`. Higher specificity required. Clear default gradients via `background-image: none; box-shadow: none;`.
- **Wayland focus flashing**: Use `load_cached()` (JSON cache), not `load_data()` (xclip/wl-paste).
- **Anti-flicker**: Wrap tab-switch placeholder changes with `handler_block()`/`handler_unblock()` on `search-changed`.
- **Restart lock order**: Release flock fd *before* `subprocess.Popen`.
- **Signal loop storms**: ListBox row removal/addition inside selection callbacks triggers recursion. Use `handler_block`/`handler_unblock`, check `row.get_parent() == listbox`, prefer in-place label updates.
- **Window stretching**: Wrap variable-length list containers in `Gtk.ScrolledWindow` with `(NEVER, AUTOMATIC)` policies.
- **Swallowed exceptions**: PyGObject callbacks swallow tracebacks. Check `~/.local/share/opencode-switcher/run.log` for NameErrors/syntax errors.
