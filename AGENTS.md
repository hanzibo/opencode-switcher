# OpenCode Switcher — Agent Instructions

Linux GTK3 desktop tray app for switching between OpenCode (CLI) sessions, clipboard history, AI assistant sidebar, and MCP tool integration. Python 3 + GTK3 + AyatanaAppIndicator. Wayland-only (X11 support was dropped).

## Commands

| Action | Command | Notes |
|--------|---------|-------|
| Run (dev) | `venv/bin/python3 main.py` | Needs `opencode` in PATH |
| Run (prod) | `./run.sh` | Log rotation, nvm, `JSC_useJIT=false` |
| Test (all) | `venv/bin/python3 -m unittest discover tests` | |
| Test (single) | `venv/bin/python3 -m unittest tests.test_mcp_integration` | Also works: `tests/test_mcp_integration.py` |
| Venv setup | `python3 -m venv --system-site-packages venv && venv/bin/pip install -r requirements.txt` | `--system-site-packages` required for system PyGObject |
| Install | `./install.sh install` | Copies to `~/.local/share/opencode-switcher/`, enables systemd, GNOME ext |
| Uninstall | `./install.sh uninstall` | Interactive — asks about keeping user data |
| Status | `./install.sh status` | Checks install, desktop entry, service, opencode CLI, GNOME ext |
| DB inspect | `venv/bin/python3 -m system.inspect_db` | Lists session table schema + latest rows |

**System deps**: `gir1.2-ayatanaappindicator3-0.1 python3-gi python3-gi-cairo python3-pip python3-venv wl-clipboard gir1.2-webkit2-4.1` — webkit2gtk NOT in `install.sh` but required at runtime (AI panel crashes without it).

**Tests ARE version-controlled**: all 51 `tests/test_*.py` files are tracked (only `tests/__pycache__/` is gitignored). No CI, no pre-commit, no pyproject — stdlib `unittest` is the only test runner, and there is no linter/formatter config (manual discipline). Run focused tests as `venv/bin/python3 -m unittest tests.test_session_store`; the venv uses `--system-site-packages` so a fresh clone needs system GTK deps installed first.

**Commit convention**: `fix(area):`, `feat(area):`, `improve(area):`, `refactor(area):`, `docs(area):`, `merge:`. Area prefix follows module (e.g., `ai-panel`, `theme`, `tool-registry`, `clipboard`, `mcp`).

## Architecture

### Entrypoint & Startup Flow

`main.py` (416 lines) is the sole entrypoint. Startup:
```
systemd/.desktop → run.sh → main.py (flock lock)
  → load_theme_config() → migrate_history.run_migration()
  → ClipboardStore → CategoryStore → SearchPanel+ClipboardPanel → HotkeyManager
  → App.run(): hotkey start → Gtk.main()
  → Ctrl+C: app.stop() → flock release
```

**Critical**: Release flock fd *before* `subprocess.Popen(self)` on restart — spawning before release makes new instance fail. `KillMode=process` in service file means systemd only kills the script process, not its children.

**Flat root** — no `__init__.py` at project level, not an importable Python package. Root `.py` files imported directly by `main.py`.

### Module Map

| Module | Lines | Role |
|--------|-------|------|
| `main.py` | 416 | Entrypoint: flock lock, App(), Gtk.main(), MCP shutdown on quit |
| `views/` | ~9230 | Main UI views (`panel.py`, `clipboard_panel.py`, `ai_chat_panel.py`, `ai_popovers.py`). `ai_chat_panel.py` is the largest file in the repo (~4600 lines, streaming + suspend/resume logic) |
| `dialogs/` | ~4550 | GTK dialogs (`settings_dialog.py`, `prompts_config_dialog.py`, `memory_manager_dialog.py`, etc.) |
| `stores/` | ~2810 | Data persistence & state (`clipboard_store.py`, `session_store.py`, `session_refresh.py`, `skill_store.py`, `theme_config.py`, `delete_queue.py`) |
| `ai_engine/` | ~1630 | AI LLM engine & rendering (`llm_client.py`, `ai_tool_loop.py`, `ai_html_template.py`, `render_pipeline.py`) |
| `system/` | ~620 | System IPC & utilities (`hotkey.py`, `launcher.py`, `event_types.py`, `migrate_history.py`, `utils.py`, `inspect_db.py`) |
| `mcp_integration/` | ~4260 | MCP protocol layer (JSON-RPC over stdio/http transports in `transports/`, OAuth 2.1 client auth in `oauth/`, `client_manager.py`, GTK asyncio bridge) |
| `tool_registry/` | ~6330 | AI tool executors — 28 tools across 13 schema modules (+`display.py`/`_state.py` helpers, no schemas) |
| `html_templates/` | ~1910 | Web assets (`chat.js`, `chat.css`) for WebKit WebView rendering |
| `ai_text_utils/` | ~1310 | Pure text/markdown/math helpers (zero GTK dep) |
| `deploy/` | — | Templated `opencode-switcher.{service,desktop}` + icon; `install.sh` substitutes `__INSTALL_DIR__` via `sed` |

### Tool Registry (`tool_registry/`)

28 AI tool executors dispatched via `TOOL_EXECUTORS` dict in `__init__.py`, assembled from per-module `TOOL_SCHEMAS` lists. Each tool call carries a `purpose` parameter (agent-generated description) displayed in the tool summary line.

Key executor modules: `common.py` (`get_current_time`, `ask_user_question`), `bash.py` (persistent bash session, sentinel protocol, interactive command blocking), `web.py` (`web_search`/`web_fetch` with Obscura browser), `filesystem.py` (safe-path guarded), `subagent.py` (parallel isolated execution), `memory.py` (long-term semantic memory → `agent_memory.json`), `mail.py` (read_qq_mail with credential caching), `gmail.py` (OAuth2 Gmail via `google-api-python-client`, creds in `gmail_credentials/`), `notification.py` (`send_notification`), `todo.py` (todo store → `todos.json`), `search.py`, `skill.py` (`read_skill`), `code_analysis.py` (`get_code_metrics`/`find_project_dependencies`/`parse_file_ast`).

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

- **`terminate_web_process()` is the only effective memory release** for WebKit — `load_html` + cache clearing barely moves the ~200MB WebProcess RSS.
- **MemoryPressureSettings must be set at `WebContext` construction time** — runtime changes are ignored. Configured in `ai_engine/ai_html_template.py:188-209` (`get_shared_web_context()`): 300MB limit, 5s poll, 0.2/0.4 thresholds; constants duplicated at `views/ai_chat_panel.py:58-61`.
- **Shared WebContext singleton**: all WebViews use `get_shared_web_context()` to avoid duplicate WebKitNet processes. Reuse it (never create a fresh context) when rebuilding a WebView.
- **Suspend flow** (`views/ai_chat_panel.py`): 60s after panel hidden (`_SUSPEND_DELAY_SECONDS`), deferred while any conversation is still streaming → cache `_last_rendered_html` into `_ai_html_cache[conv_id]`, set `_webview_suspended`, then `terminate_web_process()`.
- **First-open guard (`_ai_has_shown`)**: the suspend timer never starts until the panel has been shown to the user at least once (set in `open_ai_and_load_recent`/`show_panel`/`start_new_conversation`/`ask_llm_api`, NOT in `on_panel_shown`). Without it, the startup-time internal hide would kill the cold-spawned WebProcess and the first Ctrl+Shift+X after an OS reboot would pay a 1-2s cold spawn. **WebProcess spawn is the bottleneck**: measured ~2100ms first spawn vs ~165ms warm respawn vs ~60ms HTML load (Python side only ~40ms).
- **Cold-start preload**: `system/utils.py:preload_webkit_libs()` (called from `main.py` startup thread) issues `posix_fadvise(WILLNEED)` on `libwebkit2gtk-4.1`/`libjavascriptcoregtk-4.1` (~120MB) + WebProcess binaries to warm the OS page cache after reboot. Best-effort, never blocks startup.
- **Deferred recent-load**: `open_ai_and_load_recent` shows the panel synchronously, then loads the recent conversation via `GLib.idle_add(_load_recent_conversation_deferred)` (guarded by `_ai_recent_load_pending` + `get_visible()`) so the first frame paints before any spawn/render work.
- **Resume flow**: `on_panel_shown` → `load_html(theme_template + cached_html)`, reset `_streaming_container_created` (DOM must be rebuilt).
- **Crash vs suspend**: `_on_webview_crashed` skips the rebuild when `_webview_suspended` is set (intentional termination, not a crash); on real crashes it builds a new `WebKit2.WebView` reusing the shared context.

## Key Features & Quirks

### Themes
All theme colors live in `stores/theme_config.py`: `_THEMES = {"light", "dark", "dark-moon"}`. Views read colors only via `get_theme()` / `get_panel_css_vals()` / `parse_css_rgba()` — do NOT reintroduce per-view color dicts (a recent refactor consolidated the duplicates into `theme_config.py`). Adding a theme means updating `_THEMES` **and** every key `get_panel_css_vals()` returns: views index the dict directly, and a missing key raises `KeyError` (see `cat_hover` and `panel_bg`/`panel_title` fix history).

### Slash Commands
- **Search bar** (`views/panel.py`): `/new`, `/open`, `/gm <query>`, `/google <query>`. Tab-completion. `/gm` uses `evdev.UInput` for automated typing simulation. Delay: 1.2s (Firefox running) / 4.0s (not).
- **AI chat input** (`_AI_COMMANDS` in `views/ai_chat_panel.py`): `/new`, `/delete`, `/fork`, `/retry`, `/rollback`, `/title`, `/model`, `/cd`, `/summary keep=N` (default 50), `/skill`. **`views/clipboard_panel.py` has its own shorter `_AI_COMMANDS` subset** (6 entries, no `/fork`, `/summary`, `/skill`) — both lists must stay in sync with the ai_chat_panel version. `/fork` branches the current conversation via `ConversationStore.fork_conversation()` and copies the source conversation's system prompt snapshot.

### System Prompt (per-conversation snapshot)
Global default lives in `AISettingsStore().system_prompt` (`ai_settings.json`). On new conversation creation, the current global value is snapshotted into `conv.system_prompt` (`_snapshot_system_prompt()` in `views/ai_chat_panel.py`). Later edits to the global setting do **not** affect existing conversations — each conversation keeps the prompt it was started with, persisted in the conversation JSON. Test coverage: `tests/test_system_prompt.py`.

### Clipboard Classification
Heuristic regex scoring in `clipboard_store.py` (`classify_text()`, `detect_language_name()`). **Duplicated in `gnome-extension/extension.js`** — ~150 lines of scoring in both Python and JS. Must update both for any classification change.

### Template/Dynamic Copy
- `${&}` embeds clipboard content. `\${&}` → literal `${&}`.
- Multi‑parameter: `${index:prompt=default}`
- `TEMPLATE_REGEX` duplicated in `dynamic_copy_dialog.py`, `clipboard_panel.py`, `ai_chat_panel.py` — must keep in sync.

### AI Assistant Streaming Architecture
- Three-zone DOM structure in WebView (`.bubble-region` for reasoning/tool/answer).
- Streaming: `_render_current_assistant_message()` calls JS `updateMessageContainer()` to incrementally update only the answer region. Poll interval: 200ms.
- Token batching: Python buffers streaming tokens and flushes to JS every `_BATCH_FLUSH_MS`=60ms (`ai_chat_panel.py:153`, `_flush_token_buffer`) — a batching perf mechanism, distinct from the JS-side 200ms polling.
- Non-streaming (conversation switch, full rebuilds): `_render_markdown()`.
- **DOM Windowing**: Only last 10 rounds visible. `display:none` for older. Batch-load 3 more per click.
- Background conversations: not interrupted by hiding panel or switching conversations. State cached in `self._ai_running_convs` by `conv_id`.

### AI Input: Multi-line Preservation
`_preserve_newlines()` preserves Shift+Enter line breaks, avoiding `<br>` inside fenced code blocks.

### Subagent Status Bar Flash Guard
Dynamically add/remove `.subagent-status-bar` class on FlowBox before `hide()`/`.remove(child)` to avoid GTK3 layout flicker.

### Subagent Monitoring & Iteration Limit
- Subagent bubbles show real-time ReAct action status (Thinking / Tool Call: `<tool>` / Answering) via `tool_registry/subagent.py` — event-driven, no fixed turn cap.
- Subagent depth is capped only by `AISettingsStore().max_tool_iterations` (shared with main agent; default 25, fallback 25 on corrupt/None values). The old `max_turns` parameter was removed — subagents run until the model emits a plain-text answer or hits the cap. Changing the setting affects subagent depth.

### SQLite Database Coupling
- **DB**: `~/.local/share/opencode/opencode.db`. Connection: `timeout=5`, `PRAGMA journal_mode=WAL`.
- **Exclude**: archived sessions, subagent sessions (`title LIKE '%(@%subagent)%'`), non-existent dirs.
- **Live detection**: `pgrep -f opencode` → scan `/proc/<pid>/cmdline` + `/proc/<pid>/cwd`. Filters out switcher itself.
- **Status**: "live" (running), "recent" (<24h), "closed".
- **Hard delete**: `delete_session()` (`session_store.py:279`) shells out to `opencode session delete <session_id>` first (cross-verifies the row is gone), falling back to `_soft_delete()` (line 254, sets `time_archived`) if the CLI is missing or fails — instead of only hiding rows.
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
| `~/.config/opencode-switcher/config.json` | Theme setting (light/dark/dark-moon) |
| `~/.config/opencode-switcher/clipboard_history.json` | 150 FIFO clipboard items (+ `.backup`) |
| `~/.config/opencode-switcher/categories.json` | Custom categories + recycle bin |
| `~/.config/opencode-switcher/custom_prompts.json` | Named prompts |
| `~/.config/opencode-switcher/llm_settings.json` | LLM API keys (perms `0o600`) |
| `~/.config/opencode-switcher/ai_settings.json` | AI truncation (`soft_limit`, `trim_target`) + `system_prompt` + `max_tool_iterations` |
| `~/.config/opencode-switcher/agent_memory.json` | Long-term semantic memory |
| `~/.config/opencode-switcher/gmail_credentials/` | `credentials.json` + `token.json` (token `0o600`) |
| `~/.config/opencode-switcher/qq_mail_credentials.json` | QQ mail IMAP credentials |
| `~/.config/opencode-switcher/mcp_servers.json` | MCP server configs (perms `0o600`) |
| `~/.config/opencode-switcher/todos.json` | Todo tool store |
| `~/.config/opencode-switcher/skills/` | AI skill store |
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
- **No linter/formatter/CI**: manual discipline; validate changes with `unittest`.

## Postmortem Summary: `data-tool-call-id` Broke Tool Card Markdown

When Phase 3a added `data-tool-call-id` to `<details class="tool-step-details">`, the regex in `_escape_tool_results` (`ai_text_utils/markdown.py:96`) expected `>` immediately after the class attribute and failed to match → tool card HTML wasn't replaced by placeholder before markdown pass → raw triple backticks inside tool results leaked across messages.

**Fix**: Changed regex from `(<details class="tool-step-details">` to `(<details class="tool-step-details".*?>` to allow arbitrary attributes.

**Lesson**: Any regex matching generated HTML must be updated when the HTML template changes. Searchable marker: `# ponytail: _escape_tool_results pattern coupling`.

## Reference

- `.hzb-agents/experience/` — 130 per-feature postmortems (**gitignored local dir**)
- `.omo/plans/` — 49 structured work plans (**gitignored local dir**)
- `docs/plans/` — recent audit/refactor plans (e.g., `/fork` review, subagent monitoring)
- `gnome-extension/AGENTS.md` — GNOME Shell extension internals
