# OpenCode Switcher — Agent Instructions

Linux GTK3 desktop tray app for switching between OpenCode (CLI) sessions, clipboard history, AI assistant sidebar, and MCP tool integration. Python 3 + GTK3 + AyatanaAppIndicator.

## Commands

| Action | Command | Notes |
|--------|---------|-------|
| Run (dev) | `venv/bin/python3 main.py` | Needs `opencode` in PATH |
| Run (prod) | `./run.sh` | Log rotation, nvm, `JSC_useJIT=false` |
| Test (all) | `venv/bin/python3 -m unittest discover tests` | |
| Test (single) | `venv/bin/python3 -m unittest tests/test_mcp_integration.py` | |
| Venv setup | `python3 -m venv --system-site-packages venv && venv/bin/pip install -r requirements.txt` | `--system-site-packages` required for system PyGObject |
| Install | `./install.sh install` | Copies to `~/.local/share/opencode-switcher/`, enables systemd, GNOME ext |
| Uninstall | `./install.sh uninstall` | Interactive — asks about keeping user data |
| Status | `./install.sh status` | Checks install, desktop entry, service, opencode CLI, GNOME ext |
| DB inspect | `venv/bin/python3 -m system.inspect_db` | Lists session table schema + latest rows |

**System deps**: `gir1.2-ayatanaappindicator3-0.1 python3-gi python3-gi-cairo python3-pip python3-venv wl-clipboard gir1.2-webkit2-4.1` — webkit2gtk NOT in `install.sh` but required at runtime (AI panel crashes without it).

**`tests/` is gitignored**: only `test_conversation_fork.py` is tracked (pre-ignore). Most test files exist locally but are NOT in version control — don't rely on `git` to find them, and don't be surprised when new test files don't show up in `git status`.

**Commit convention**: `fix(area):`, `feat(area):`, `improve(area):`, `refactor(area):`, `docs(area):`, `merge:`. Area prefix follows module (e.g., `ai-panel`, `theme`, `tool-registry`, `clipboard`, `mcp`).

## Architecture

### Entrypoint & Startup Flow

`main.py` is the sole entrypoint. Startup:
```
systemd/.desktop → run.sh → main.py (flock lock)
  → _load_config() → migrate_history.run_migration()
  → ClipboardStore → CategoryStore → SearchPanel+ClipboardPanel → HotkeyManager
  → App.run(): hotkey start → Gtk.main()
  → Ctrl+C: app.stop() → flock release
```

**Critical**: Release flock fd *before* `subprocess.Popen(self)` on restart — spawning before release makes new instance fail. `KillMode=process` in service file means systemd only kills the script process, not its children.

**Flat root** — no `__init__.py` at project level, not an importable Python package. Root `.py` files imported directly by `main.py`.

### Module Map

| Module | Lines | Role |
|--------|-------|------|
| `main.py` | 261 | Entrypoint: flock lock, App(), Gtk.main() |
| `views/` | ~8300 | Main UI views (`panel.py`, `clipboard_panel.py`, `ai_chat_panel.py`, `ai_popovers.py`) |
| `dialogs/` | ~4200 | GTK dialogs (`settings_dialog.py`, `prompts_config_dialog.py`, `memory_manager_dialog.py`, etc.) |
| `stores/` | ~2100 | Data persistence & state (`clipboard_store.py`, `session_store.py`, `skill_store.py`, `theme_config.py`) |
| `ai_engine/` | ~1350 | AI LLM engine & rendering (`llm_client.py`, `ai_tool_loop.py`, `ai_html_template.py`, `render_pipeline.py`) |
| `system/` | ~440 | System IPC & utilities (`hotkey.py`, `launcher.py`, `event_types.py`, `migrate_history.py`, `utils.py`) |
| `mcp_integration/` | ~2170 | MCP protocol layer (JSON-RPC over stdio/http transports in `transports/`, GTK asyncio bridge) |
| `tool_registry/` | 28 tools across 14 modules | AI tool executors (bash, web, filesystem, code analysis, subagent, search, etc.) |
| `html_templates/` | ~1910 | Web assets (`chat.js`, `chat.css`) for WebKit WebView rendering |
| `ai_text_utils/` | ~1270 | Pure text/markdown/math helpers (zero GTK dep) |
| `deploy/` | — | Templated `opencode-switcher.{service,desktop}` + icon; `install.sh` substitutes `__INSTALL_DIR__` via `sed` |

### Tool Registry (`tool_registry/`)

28 AI tool executors dispatched via `TOOL_EXECUTORS` dict. Assembled from per-module `TOOL_SCHEMAS` lists in `__init__.py`. Each tool call carries a `purpose` parameter (agent-generated description) displayed in the tool summary line.

Key executor modules: `bash.py` (persistent bash session, sentinel protocol, interactive command blocking), `web.py` (`web_search`/`web_fetch` with Obscura browser), `filesystem.py` (safe-path guarded), `subagent.py` (parallel isolated execution), `memory.py` (long-term semantic memory), `mail.py` (read_qq_mail with credential caching).

### Wayland Integration (GNOME Shell Extension)

The app operates exclusively on Wayland. Three IPC channels using `~/.cache/opencode-switcher/`:

| Channel | Mechanism | Direction |
|---------|-----------|-----------|
| **Clipboard capture** | GNOME ext monitors `owner-changed` → writes `clipboard_history.json` + touches `clipboard.updated` marker → Python app monitors via `Gio.FileMonitor` | GNOME ext → Python |
| **Hotkey toggle** | GNOME shortcut calls `opencode-switcher-toggle` → sends `b"toggle"`/`b"toggle_ai"` over Unix socket at `toggle.sock` → `HotkeyManager` | Shell → Python |
| **Window focus** | Python writes wm_class to `focus.request` → GNOME ext monitors via `Gio.FileMonitor` → calls `win.activate()` | Python → GNOME ext |

**`clipboard.updated` is lossy**: single-file timestamp overwritten by rapid clipboard events — not a queue.

See `gnome-extension/AGENTS.md` for full extension internals.

## Critical GTK & PyGObject Crash Guards

These cause SIGSEGV if violated. Follow strictly.

- **Signal callback safety**: Never modify widget tree hierarchy inside GTK event callbacks — destroys C-level signal source → SIGSEGV. Defer via `GLib.idle_add()`.
- **Focused widget safety**: Never destroy/remove a focused `Entry`. Call `window.set_focus(None)` first.
- **Dialog destruction trap**: Read `dialog.get_filename()` *before* `dialog.destroy()` — destroy returns None.
- **CSS Provider scope**: Use `widget.get_style_context().add_provider(...)`. `add_provider_for_screen` leaks globally (accepted tradeoff in 3 files: panel.py, clipboard_panel.py, ai_chat_panel.py).
- **Nested dialog focus guard**: `_dialog_active` flag via `on_dialog_shown`/`on_dialog_hidden`. Inner dialogs must NOT trigger `on_dialog_hidden()`.
- **GTK3 CSS limits**: No `!important`. Higher specificity required. Clear default gradients via `background-image: none; box-shadow: none;`.
- **Anti-flicker**: Wrap tab-switch placeholder changes with `handler_block()`/`handler_unblock()` on `search-changed`.
- **Signal loop storms**: ListBox row removal/addition inside selection callbacks triggers recursion. Use `handler_block`/`handler_unblock`, check `row.get_parent() == listbox`, prefer in-place label updates.
- **Swallowed exceptions**: PyGObject callbacks swallow tracebacks. Check `run.log` for NameErrors/syntax errors.
- **Thread safety**: `GLib.idle_add(callback, *args)` for background→UI updates. No `asyncio` loop directly driving GTK widgets except through `mcp_integration.gtk_asyncio_bridge`.

## WebView Memory Optimization Patterns

- **`terminate_web_process()` is the only effective memory release** for WebKit. `load_html('<html></html>')` + `clear_cache()` + `malloc_trim()` reduces only ~30MB of ~200MB WebProcess RSS.
- **MemoryPressureSettings** must be set at `WebContext` construction time (`WebKit2.WebContext.new_with_context()`). Runtime changes are ignored. Configured at `ai_chat_panel.py:258-262` — 300MB limit, 5s poll, 0.2/0.4 thresholds.
- **After terminate**, call `set_background_color(rgba)` with opaque color — terminated WebView renders transparent, showing desktop behind.
- For clean suspension: terminate → set background → clear_cache.

## Key Features & Quirks

### Slash Commands
- **Search bar** (`views/panel.py`): `/new`, `/open`, `/gm <query>`, `/google <query>`. Tab-completion. `/gm` uses `evdev.UInput` for automated typing simulation. Delay: 1.2s (Firefox running) / 4.0s (not).
- **AI chat input** (`_AI_COMMANDS` in `views/ai_chat_panel.py`): `/new`, `/delete`, `/fork`, `/retry`, `/rollback`, `/title`, `/model`, `/cd`, `/summary keep=N`, `/skill`. List duplicated in `views/clipboard_panel.py` — keep in sync. `/fork` branches the current conversation via `ConversationStore.fork_conversation()`.

### Clipboard Classification
Heuristic regex scoring in `clipboard_store.py` (`classify_text()`, `detect_language_name()`). **Duplicated in `gnome-extension/extension.js`** — ~150 lines of scoring in both Python and JS. Must update both for any classification change.

### Template/Dynamic Copy
- `${&}` embeds clipboard content. `\${&}` → literal `${&}`.
- Multi‑parameter: `${index:prompt=default}`
- `TEMPLATE_REGEX` duplicated in `dynamic_copy_dialog.py`, `clipboard_panel.py`, `ai_chat_panel.py` — must keep in sync.

### AI Assistant Streaming Architecture
- Three-zone DOM structure in WebView (`.bubble-region` for reasoning/tool/answer).
- Streaming: `_render_current_assistant_message()` calls JS `updateMessageContainer()` to incrementally update only the answer region. Poll interval: 200ms.
- Non-streaming (conversation switch, full rebuilds): `_render_markdown()`.
- **DOM Windowing**: Only last 10 rounds visible. `display:none` for older. Batch-load 3 more per click.
- Background conversations: not interrupted by hiding panel or switching conversations. State cached in `self._ai_running_convs` by `conv_id`.

### AI Input: Multi-line Preservation
`_preserve_newlines()` preserves Shift+Enter line breaks, avoiding `<br>` inside fenced code blocks.

### Subagent Status Bar Flash Guard
Dynamically add/remove `.subagent-status-bar` class on FlowBox before `hide()`/`.remove(child)` to avoid GTK3 layout flicker.

### Subagent Monitoring & Iteration Limit
- Subagent bubbles show real-time ReAct action status (Thinking / Tool Call: `<tool>` / Answering) via `tool_registry/subagent.py` — event-driven, no fixed turn cap.
- Max turns is governed by `AISettingsStore().max_tool_iterations` (shared with main agent), NOT a hardcoded constant. Changing that setting affects subagent depth.

### SQLite Database Coupling
- **DB**: `~/.local/share/opencode/opencode.db`. Connection: `timeout=5`, `PRAGMA journal_mode=WAL`.
- **Exclude**: archived sessions, subagent sessions (`title LIKE '%(@%subagent)%'`), non-existent dirs.
- **Live detection**: `pgrep -f opencode` → scan `/proc/<pid>/cmdline` + `/proc/<pid>/cwd`. Filters out switcher itself.
- **Status**: "live" (running), "recent" (<24h), "closed".
- **Known optimization**: `part` table snippet query reduced from 49,740→100 rows via `INNER JOIN + MAX(time_created)` subquery (`session_store.py`). Data transfer dropped ~97MB→0.03MB.

## Anti-Patterns (Must-Know)

- **`--system-site-packages` venv**: Breaks isolation (required for system PyGObject).
- **`run.sh` sources NVM**: Couples tray app runtime to user's shell Node.js env.
- **`opencode-switcher-toggle`**: Python code inside shell script via `exec python3 -c "..."` — fragile quoting.
- **Hardcoded version** (`VERSION="1.0.0"` in `install.sh`) — no git tags or version automation.
- **WebKit2 dependency** not in `install.sh` but required at runtime.
- **GNOME extension duplicates Python classification** — must keep both in sync manually.
- **Shared `clipboard_history.json`** — written by both Python and JS, no locking → potential corruption.
- **`TEMPLATE_REGEX` duplicated** in 3 files — must keep in sync.
- **Lossy marker IPC** (`clipboard.updated`): single-file timestamp, not a queue.
- **Image garbage buildup**: `clipboard_history.json` can reference fewer files than `images/` directory contains. Startup calls `_delete_orphan_images()` in `clipboard_store.py:_load()` to clean unreferenced PNGs.

## Config & Cache Paths

| Path | Contents |
|------|----------|
| `~/.config/opencode-switcher/config.json` | Theme setting (dark/light) |
| `~/.config/opencode-switcher/clipboard_history.json` | 150 FIFO clipboard items |
| `~/.config/opencode-switcher/categories.json` | Custom categories + recycle bin |
| `~/.config/opencode-switcher/custom_prompts.json` | Named prompts |
| `~/.config/opencode-switcher/llm_settings.json` | LLM API keys (perms `0o600`) |
| `~/.config/opencode-switcher/ai_settings.json` | AI truncation threshold (`soft_limit`, `trim_target`) |
| `~/.config/opencode-switcher/memory.json` | Long-term semantic memory (perms `0o600`) |
| `~/.config/opencode-switcher/mcp_servers.json` | MCP server configs (perms `0o600`) |
| `~/.config/opencode-switcher/lock` | Flock lock file |
| `~/.cache/opencode-switcher/toggle.sock` | Unix socket (Wayland hotkey) |
| `~/.cache/opencode-switcher/conversations/` | AI conversation JSON files |
| `~/.cache/opencode-switcher/clipboard.updated` | Marker file (Wayland clipboard IPC) |
| `~/.cache/opencode-switcher/last_written_hash` | Hash of last app-written content |
| `~/.cache/opencode-switcher/focus.request` | Focus request (Wayland) |

## Conventions

- **Strings**: double quotes (~10:1 over single). Docstrings: `"""`
- **Imports**: stdlib → third-party → local. `gi.require_version()` BEFORE `from gi.repository import ...`
- **Comments**: `# <space><text>`, Chinese or English.
- **`# ponytail:`** marks intentionally removed code — searchable breadcrumb for deleted blocks.
- **`console.error('opencode-switcher: ...')`** prefix in GNOME extension JS error messages.
- **Settings dialog**: factory pattern `show_settings_dialog(parent, on_dialog_shown, on_dialog_hidden)`. Reuses focus-guard `_dialog_active` flag.

## Postmortem Summary: `data-tool-call-id` Broke Tool Card Markdown

When Phase 3a added `data-tool-call-id` to `<details class="tool-step-details">`, the regex in `_escape_tool_results` (`ai_text_utils/markdown.py:96`) expected `>` immediately after the class attribute and failed to match → tool card HTML wasn't replaced by placeholder before markdown pass → raw triple backticks inside tool results leaked across messages.

**Fix**: Changed regex from `(<details class="tool-step-details">` to `(<details class="tool-step-details".*?>` to allow arbitrary attributes.

**Lesson**: Any regex matching generated HTML must be updated when the HTML template changes. Searchable marker: `# ponytail: _escape_tool_results pattern coupling`.

## Reference

- `.hzb-agents/experience/` — 130 per-feature postmortems
- `.omo/plans/` — 49 structured work plans
- `docs/plans/` — recent audit/refactor plans (e.g., `/fork` review, subagent monitoring)
- `gnome-extension/AGENTS.md` — GNOME Shell extension internals
