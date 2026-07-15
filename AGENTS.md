# OpenCode Switcher — Agent Instructions

Linux GTK3 desktop tray app for switching between OpenCode (CLI) sessions, clipboard history management, and an AI assistant sidebar. Python 3 + GTK3 + AyatanaAppIndicator. **No CI/linter/formatter/typechecker. No automated tests.** 507 commits, all by single author.

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

**System deps** (beyond pip): `gir1.2-ayatanaappindicator3-0.1 python3-gi python3-pip python3-venv wl-clipboard gir1.2-webkit2-4.1` — webkit2gtk is NOT in `install.sh` but required at runtime (AI panel crashes without it).

**Commit convention**: `fix(area):`, `feat(area):`, `improve(area):`, `refactor(area):`, `optimize(area):`, `perf(area):`, `style(area):`, `docs(area):`, `merge:`. Area prefix follows module (e.g., `ai-panel`, `theme`, `tool-registry`, `clipboard`).

## Architecture

### Entrypoint & Startup Flow

`main.py` (~288 lines) is the sole entrypoint. Startup sequence:
```
systemd/.desktop → run.sh → main.py (flock lock)
  → _load_config() → migrate_history.run_migration() (from `migrate_history.py`)
  → ClipboardStore → CategoryStore → SearchPanel+ClipboardPanel → HotkeyManager
  → App.run(): hotkey start → Gtk.main()
  → Ctrl+C: app.stop() → flock release
```
**Critical**: Release flock fd *before* `subprocess.Popen(self)` on restart — spawning before release makes new instance fail. `KillMode=process` in service file means systemd only kills the script process, not its children.

### Module Roles

| File | Lines | Role |
|------|-------|------|
| `main.py` | 288 | Entrypoint: flock lock, App(), Gtk.main() |
| `ai_chat_panel.py` | 3329 | AI assistant — WebView, ReAct loops, background threads, subagent UI |
| `clipboard_panel.py` | 2137 | Clipboard panel — assembles subcomponents + event routing |
| `panel.py` | 1369 | Search panel — tab switcher, slash cmds, CSS-in-code, session list |
| `clipboard_store.py` | 1272 | God module — clipboard, categories, conversations, memory (`MemStore`), prompts |
| `ai_html_template.py` | 1682 | WebView HTML template + KaTeX + lightbox JS + DOM Windowing + 3-zone streaming |
| `prompts_config_dialog.py` | 910 | Prompt/category management dialog |
| `ai_text_utils.py` | 1047 | Pure markdown/math/vision helpers (zero GTK dep), 3 sub-renderers for streaming |
| `ai_popovers.py` | 522 | AI command autocomplete + conversation history popover |
| `settings_dialog.py` | 427 | Settings dialog layout and fields |
| `llm_client.py` | 368 | LLM HTTP client + `_ToolCallAccumulator` for SSE delta merge |
| `gnome-extension/` | 350 | GNOME Shell extension (Wayland clipboard + focus IPC) |
| `sort_cats_dialog.py` | 329 | Category drag-and-drop sorting dialog |
| `sort_dialog.py` | 277 | Clipboard item drag-and-drop sorting dialog |
| `recycle_bin_dialog.py` | 240 | Recycle bin browsing/restore/delete dialog |
| `ai_tool_loop.py` | 231 | ReAct loop orchestrator — LLM call + tool iteration execution |
| `session_store.py` | 202 | SQLite reader + live-session detection via `/proc` |
| `memory_manager_dialog.py` | 193 | Semantic memory CRUD and search dialog |
| `prompt_dialog.py` | 77 | Prompt create/edit dialog |
| `launcher.py` | 75 | Terminal detection + session spawner |
| `hotkey.py` | 66 | Unix socket server for GNOME Extension hotkey toggling (Wayland) |
| `migrate_history.py` | 59 | Clipboard history type migration runner (startup) |
| `utils.py` | 49 | Utility helpers (`is_wayland`, `relative_time`, `request_window_focus`) |
| `docs/` | 2 files | Usage guide (`docs/usage.md`) + agent tools roadmap (`docs/agent-tools-roadmap.md`) |
| `tool_registry/` | 28 tools across 14 modules | AI tool executors (see below) |

**Flat root** — no `__init__.py` at project level, not an importable Python package. All `.py` files imported directly by `main.py`.

### Tool Registry (`tool_registry/`)

28 AI tool executors dispatched via `TOOL_EXECUTORS` dict. Assembled from per-module `TOOL_SCHEMAS` lists in `__init__.py`. Key modules:
- `bash.py` — persistent bash session, sentinel protocol, interactive command blocking (hard blocks: `vi`, `less`, `top`, `ssh`; conditional: `ssh-keygen`, `openssl`, `gpg`)
- `web.py` — `web_search`/`web_fetch` with Obscura browser + trafilatura extraction, `cancel_event` support
- `filesystem.py` — safe-path guarded read/write/edit/delete/rename
- `code_analysis.py` — `get_code_metrics`, `find_project_dependencies`, `parse_file_ast`
- `subagent.py` — `sub_agent`, `get_subagent_status` — parallel isolated execution
- `search.py` — `grep_search`, `glob_find`
- `todo.py` — persistent task management with dependency tracking
- `mail.py` — `read_qq_mail` for IMAP mailbox reading with credential caching
- `memory.py` — `memory_save`, `memory_list`, `memory_recall` for long-term semantic memory storage
- `display.py` — tool call/result HTML formatting for WebView
- `common.py` — `get_current_time`, `ask_user_question`
- `notification.py` — `send_notification`
- `_state.py` — shared state flags (e.g., `_TOOL_CANCELLED`)

Tool calls: LLM streams → `_ToolCallAccumulator` merges SSE deltas → `execute_tool_call()` dispatches → result fed back as `role: "tool"`. Cancel via `cancel_event` threading.Event.

Each tool call carries a `purpose` parameter (agent-generated description), displayed in the tool summary line alongside file/query/URL (via `_TOOL_DISPLAY_FIELD` mapping in `ai_text_utils.py` — 13 tools covered, module-level constant).

### Wayland Integration (GNOME Shell Extension)

The application operates exclusively on Wayland:
- **Clipboard Capture**: The GNOME Shell extension monitors clipboard `owner-changed` events, writes updates to the shared cache, and touches `~/.cache/opencode-switcher/clipboard.updated`. The Python app monitors this file marker using `Gio.FileMonitor` to trigger `load_cached()`.
- **Hotkey Toggle**: GNOME Shell shortcut calls `opencode-switcher-toggle`, which sends a `b"toggle"` or `b"toggle_ai"` message over a Unix Domain Socket at `~/.cache/opencode-switcher/toggle.sock` to the Python app's `HotkeyManager`.
- **Window Focus**: The Python app writes the target window class to `~/.cache/opencode-switcher/focus.request`, which the GNOME Shell extension monitors and activates using `win.activate()`.

**Wayland clipboard.updated marker**: Single-file timestamp overwritten by rapid clipboard events — not a queue. Lossy.

See `gnome-extension/AGENTS.md` for full extension internals.

## Key Features & Quirks

### Hotkeys
- **Ctrl+Shift+Space**: Toggle main panel (managed by GNOME Shell extension shortcut)
- **Ctrl+Shift+X**: Toggle AI panel directly (managed by GNOME Shell extension shortcut)
- Socket messages: `b"toggle"` or `b"toggle_ai"`

### Slash Commands (in search bar)
`/new`, `/open`, `/gm <query>`, `/google <query>`. Tab-completion. `/gm` uses `evdev.UInput` for automated typing simulation. Delay: 1.2s (Firefox running) / 4.0s (not).

### Clipboard Classification
Heuristic regex scoring in `clipboard_store.py` (`classify_text()`, `detect_language_name()`). **Duplicated in `gnome-extension/extension.js`** — ~150 lines of scoring in both Python and JS. Must update both for any classification change.

### Template/Dynamic Copy
- `${&}` embeds clipboard content. `\${&}` → literal `${&}`.
- Multi‑parameter: `${index:prompt=default}`
- `TEMPLATE_REGEX` duplicated in `dynamic_copy_dialog.py`, `clipboard_panel.py`, `ai_chat_panel.py` — must keep in sync.

### AI Assistant (WebKit2 WebView)
- OpenAI-compatible API. Config: `~/.config/opencode-switcher/llm_settings.json` — **saved with `0o600`** (API keys).
- WebKit settings: WebGL/HTML5 DBs/localStorage disabled (memory).
- **MemoryPressureSettings**: Configured at `ai_chat_panel.py:258-262` — 300MB limit, 5s poll, 0.2/0.4 conservative/strict thresholds. Applied via `WebKit2.WebContext(memory_pressure_settings=mps)`. Must be set at context creation — no runtime update.
- **WebView Suspension**: Suspends by first calling `terminate_web_process()` (releases ~200MB WebKit RSS), then `clear_cache()` and `load_html("about:blank")`. Do NOT skip terminate — `load_html("")` alone barely reduces memory.
- Conversation files: `~/.cache/opencode-switcher/conversations/` (JSON).
- `/cd <path>` command switches active bash cwd in AI panel.
- Subagent status bar: real-time status blocks with click-selection, adaptive polling lifecycle.
- Conversation HTML caching: history switching renders from cached HTML instead of re-rendering.
- Truncation threshold configurable via Settings UI (`soft_limit`/`trim_target` in `~/.config/opencode-switcher/ai_settings.json`).
- Tool calls display `purpose` description + file path in summary line via `_TOOL_DISPLAY_FIELD` + `_render_tool_step()`.
- **Streaming rendering architecture** (Phase 1→3a): Three-zone DOM structure (`.bubble-region` for reasoning/tool/answer). `_render_current_assistant_message()` calls JS `updateMessageContainer()` to incrementally update only the answer region during streaming. `_render_active_turn_to_html()` in `ai_text_utils.py` is a wrapper calling 3 sub-renderers (`_render_reasoning_html`, `_render_tool_steps_html`, `_render_answer_html`). `_render_markdown()` is only used for non-streaming final renders (conversation switching, full rebuilds). Poll interval: 200ms. No full DOM rebuild at stream end — final tokens delivered via 兜底渲染 that sets `window._isStreaming=false` then calls `updateMessageContainer`.
- **DOM Windowing**: Keeps only the last 10 conversation rounds visible (older ones hidden with `display:none`). Batch-loads 3 more rounds per click. Controlled by JS functions `applyWindowing()`/`showOlderBatch()`/`showAllMessages()`/`updateShowOlderBar()` in `ai_html_template.py`. CSS classes: `.msg-windowed`, `.show-older-bar`. Injected into 5 key lifecycle functions.

### AI Input: Multi-line Preservation
When re-rendering chat after AI responds, the original Shift+Enter line breaks in user messages are preserved via `_preserve_newlines()`. This function detects fenced code blocks to avoid adding `<br>` inside them.

### AI Background Concurrency & Multi-Conversation
- Chat streaming and tool loops run concurrently in the background. Hiding the panel or starting a new conversation does **not** interrupt them.
- Active background states are cached in `self._ai_running_convs` by `conv_id`.
- Switching back to a running conversation automatically restores state, shows spinners, and restarts the polling loop via `_poll_stream_queue`.
- Background completions dynamically save to disk and update dropdown titles.
- Increment `self._ai_request_id` during switches to prevent old threads from corrupting the active viewport.

### Subagent Status Bar Flash Guard
Dynamically add/remove `.subagent-status-bar` class on `self._ai_subagent_bar` FlowBox before `hide()` and `.remove(child)` to avoid visual gray-blue flashing in GTK3 due to layout recalculations.

### Semantic Memory (MemStore)
- Long-term memory query/recall tools (`memory_save`, `memory_list`, `memory_recall`) using `MemStore` (with BM25 ranking and `jieba` tokenizer fallback).
- Storage file is `~/.config/opencode-switcher/memory.json` (0o600).
- Main panel settings dialog tab for managing memory via `memory_manager_dialog.py`.

### SQLite Database Coupling
- **DB**: `~/.local/share/opencode/opencode.db`. Connection: `timeout=5`, `PRAGMA journal_mode=WAL`.
- **Exclude**: archived sessions, subagent sessions (`title LIKE '%(@%subagent)%'`), non-existent dirs.
- **Live detection**: `pgrep -f opencode` → scan `/proc/<pid>/cmdline` + `/proc/<pid>/cwd`. Filters out switcher itself. Also checks `--session` flag.
- **Status**: "live" (running), "recent" (<24h), "closed".
- **Known optimization**: `part` table snippet query was reduced from 49,740→100 rows via `INNER JOIN + MAX(time_created)` subquery (`session_store.py`). Data transfer dropped ~97MB→0.03MB.

## Config & Cache Paths

| Path | Contents |
|------|----------|
| `~/.config/opencode-switcher/config.json` | Theme setting (dark/light) |
| `~/.config/opencode-switcher/clipboard_history.json` | 150 FIFO clipboard items |
| `~/.config/opencode-switcher/categories.json` | Custom categories + recycle bin |
| `~/.config/opencode-switcher/custom_prompts.json` | Named prompts |
| `~/.config/opencode-switcher/llm_settings.json` | LLM API keys (perms 0o600) |
| `~/.config/opencode-switcher/ai_settings.json` | AI truncation threshold (`soft_limit`, `trim_target`) |
| `~/.config/opencode-switcher/memory.json` | Long-term semantic memory (perms 0o600) |
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
- **Image garbage buildup**: `clipboard_history.json` can reference fewer files than `images/` directory contains. Startup calls `_delete_orphan_images()` in `clipboard_store.py:_load()` to clean unreferenced PNGs.

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

## WebView Memory Optimization Patterns

Hard-earned from the optimization branch. Apply these when touching WebView lifecycle:

- **`terminate_web_process()` is the only effective memory release** for WebKit. `load_html('<html></html>')` + `clear_cache()` + `malloc_trim()` reduces only ~30MB of ~200MB WebProcess RSS.
- **MemoryPressureSettings** must be set at `WebContext` construction time (`WebKit2.WebContext.new_with_context()`). Runtime changes are ignored.
- **After terminate**, call `set_background_color(rgba)` with opaque color — terminated WebView renders transparent, showing desktop behind.
- For clean suspension: terminate → set background → clear_cache.

## Postmortem: `data-tool-call-id` Broke Tool Card Markdown Protection

### Symptom
After Phase 3a (incremental tool card rendering) shipped, tool results containing triple backticks in their content (e.g., `read_file` returning a file line like ` ``` `) caused the AI panel to render incorrectly — code fences from different messages paired across the conversation, mangling the display.

Affected scenarios: conversation history switching, `/retry`, and any conversation reload that hit a cached turn with tool results containing backtick-only lines.

### Root Cause Chain

```
Phase 3a A1: _render_tool_step added data-tool-call-id to <details>
  <details class="tool-step-details">
    → <details class="tool-step-details" data-tool-call-id="call_xxx">

_escape_tool_results (ai_text_utils/markdown.py:96) regex became stale:
  pattern2 = r'(?:^|\n)(<details class="tool-step-details">.*?<!-- tool-step-marker -->)'
  The regex expects '>' immediately after class="tool-step-details".
  With the new attribute, the pattern FAILS TO MATCH.

Consequence: tool card HTML (<details class="tool-step-details">...</details>)
  is no longer replaced by a placeholder before the markdown pass.
  Raw triple backticks inside the tool card's <pre><code> leak into
  the markdown renderer, where they pair with other code fences
  in the same markdown="1" div → rendering corruption.

Why it passed initial testing: the test conversations happened not to
  contain tool results with bare triple backtick lines. The bug only
  manifests when a tool result ends with or contains ``` (common in
  read_file results).
```

### Fix
**Primary**: Updated `_escape_tool_results` pattern in `ai_text_utils/markdown.py:96` to allow arbitrary attributes on `<details>`:
```python
# Before (exact match — breaks with any attribute):
pattern2 = r'(?:^|\n)(<details class="tool-step-details">.*?<!-- tool-step-marker -->)'
# After (.*? before > allows data-tool-call-id and future attributes):
pattern2 = r'(?:^|\n)(<details class="tool-step-details".*?>.*?<!-- tool-step-marker -->)'
```

**Secondary** (belt-and-suspenders): Added `_close_unclosed_code_blocks()` calls in `_render_answer_html` and `_render_reasoning_html` to fence off cross-message code fence pairing in case the placeholder protection ever fails again.

### Lesson
**Any regex that matches generated HTML must be updated when the HTML template changes.** The `_escape_tool_results` regex in `markdown.py` is a fragile coupling point — it knows the exact string emitted by `_render_tool_step` in `render.py`. When modifying HTML output in one module, always audit regex patterns in other modules that consume that HTML.

Searchable marker: `# ponytail: _escape_tool_results pattern coupling`

## Reference

- `.hzb-agents/experience/` — 128 per-feature postmortems (pitfalls, solutions, reasoning)
- `.omo/plans/` — 48 structured work plans from past development
- `gnome-extension/` — GNOME Shell extension for Wayland clipboard + focus IPC. See `gnome-extension/AGENTS.md`.
