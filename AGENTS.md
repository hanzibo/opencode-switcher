# OpenCode Switcher — Agent Instructions

Linux GTK3 tray app for switching OpenCode CLI sessions, clipboard history, AI sidebar, MCP tools. Python 3 + GTK3 + AyatanaAppIndicator. Wayland-only.

## Commands

| Action | Command | Notes |
|--------|---------|-------|
| Run (dev) | `venv/bin/python3 main.py` | Needs `opencode` in PATH |
| Run (prod) | `./run.sh` | Log rotation, nvm, `JSC_useJIT=false` |
| Test (all) | `venv/bin/python3 -m unittest discover tests` | 58 tests, stdlib `unittest` only |
| Test (single) | `venv/bin/python3 -m unittest tests.test_session_store` | Also `tests/test_mcp_integration.py` form |
| Venv setup | `python3 -m venv --system-site-packages venv && venv/bin/pip install -r requirements.txt` | `--system-site-packages` required for system PyGObject |
| Install | `./install.sh install` | To `~/.local/share/opencode-switcher/`, systemd + GNOME ext |
| Uninstall | `./install.sh uninstall` | Interactive; flags `--keep-venv`, `--purge`, `--keep-data` |
| Status | `./install.sh status` | Checks install, service, opencode CLI, GNOME ext |
| DB inspect | `venv/bin/python3 -m system.inspect_db` | Schema + latest session rows |

System deps: `gir1.2-ayatanaappindicator3-0.1 python3-gi python3-gi-cairo python3-pip python3-venv wl-clipboard` + `gir1.2-webkit2-4.1` (fallback `4.0` on older distros; `install.sh` resolves 4.1→4.0, runtime probes same order). WebKit missing = AI panel crash, not caught by `install.sh`.

No CI / pre-commit / `pyproject.toml` / linter — manual discipline. Commit: `fix(area):`, `feat(area):`, `improve(area):`, `refactor(area):`, `docs(area):`, `merge:` (area = module name).

## Architecture

**Entrypoint** `main.py` (~426 lines): `systemd/.desktop → run.sh → main.py (flock)` → `load_theme_config() → migrate_history → ClipboardStore/CategoryStore → SearchPanel+ClipboardPanel → HotkeyManager → Gtk.main()`. Flat root, no `__init__.py` — root `.py` files imported directly.

**Critical:** release flock fd *before* `subprocess.Popen(self)` on restart or new instance fails. `KillMode=process` in service file — systemd only kills the script, not children.

| Module | Role |
|--------|------|
| `views/` | UI: `panel.py`, `clipboard_panel.py`, `ai_chat/` package (9 files) + `ai_chat_panel.py` facade, `ai_popovers.py` |
| `dialogs/` | GTK dialogs: `settings/` package (9 files, 8 tab mixins) + `settings_dialog.py` facade |
| `stores/` | Persistence: `clipboard_store.py`, `session_store.py`, `session_refresh.py`, `theme_config.py`, `delete_queue.py`, `skill_store.py` |
| `ai_engine/` | LLM + rendering: `llm_client.py`, `ai_tool_loop.py`, `ai_html_template.py`, `render_pipeline.py` |
| `system/` | IPC/utils: `hotkey.py`, `launcher.py`, `migrate_history.py`, `utils.py`, `inspect_db.py` |
| `mcp_integration/` | MCP JSON-RPC over stdio/http, OAuth 2.1, GTK asyncio bridge |
| `tool_registry/` | 28 tools across ~13 schema modules, dispatched via `TOOL_EXECUTORS` in `__init__.py` (assembled from `TOOL_SCHEMAS`) |
| `ai_text_utils/` | Pure helpers (zero GTK dep): markdown/math/classifier |
| `html_templates/` / `katex/` | WebView assets (`chat.js`, `chat.css`, KaTeX) |
| `deploy/` | Templated `opencode-switcher.{service,desktop}` — `install.sh` substitutes `__INSTALL_DIR__` via `sed` |

**Wayland IPC** (`~/.cache/opencode-switcher/`):

| Channel | Mechanism | Direction |
|---------|-----------|-----------|
| Clipboard | GNOME ext `owner-changed` → writes `clipboard_history.json` + touches `clipboard.updated` → Python `Gio.FileMonitor` | ext → Python |
| Hotkey | `opencode-switcher-toggle` → `b"toggle"`/`b"toggle_ai"` over `toggle.sock` → `HotkeyManager` | shell → Python |
| Focus | Python writes wm_class to `focus.request` → ext `Gio.FileMonitor` → `win.activate()` | Python → ext |

`clipboard.updated` is lossy (single file, rapid events overwrite). `gnome-extension/AGENTS.md` has extension internals.

## Gotchas — Do Not Break

**GTK SIGSEGV guards:**
- Never modify widget hierarchy inside signal callbacks — defer via `GLib.idle_add()`.
- Never destroy/remove a focused `Entry` — `window.set_focus(None)` first.
- Read `dialog.get_filename()` *before* `dialog.destroy()`.
- `widget.get_style_context().add_provider()` not `add_provider_for_screen` (global leak; 3-file exception is intentional).
- Nested dialogs: `_dialog_active` flag via `on_dialog_shown`/`on_dialog_hidden` — inner dialogs must not clear it.
- GTK3 CSS: no `!important`, clear gradients with `background-image: none; box-shadow: none;`.
- ListBox row removal in selection callbacks → recursion; use `handler_block`/`handler_unblock`, check `row.get_parent() == listbox`.
- PyGObject callbacks swallow tracebacks — check `run.log`.
- Background→UI via `GLib.idle_add()`; only `mcp_integration.gtk_asyncio_bridge` drives `asyncio` into GTK.

**WebView / WebKit:**
- Only `terminate_web_process()` actually frees ~200MB WebProcess RSS; `load_html` + cache clear does not.
- `MemoryPressureSettings` must be set at `WebContext` construction (`ai_engine/ai_html_template.py:get_shared_web_context()`, 300MB/5s/0.2/0.4); runtime changes ignored. All WebViews must reuse the singleton — never create a fresh context.
- Suspend: 60s after panel hidden, deferred while streaming, caches `_last_rendered_html` then `terminate_web_process()`. Guard `_ai_has_shown` prevents startup hide from killing cold spawn. Resume via `load_html(theme + cached_html)`.
- `_on_webview_crashed` must skip rebuild when `_webview_suspended`.

**Duplication traps (must keep in sync):**
- `ai_text_utils/classifier.py: classify_text()/detect_language_name()` ↔ `gnome-extension/extension.js: classifyText()` (~150 lines).
- `TEMPLATE_REGEX` in `dialogs/dynamic_copy_dialog.py`, `views/clipboard_panel.py`, `views/ai_chat/constants.py`.
- `_AI_COMMANDS` in `views/ai_chat/constants.py` (full) ↔ `views/clipboard_panel.py` (shorter subset, 6 entries).
- Theme colors: `stores/theme_config.py: _THEMES` + `get_panel_css_vals()` — every key must exist or views `KeyError`.

**Other:**
- Themes: views read only via `get_theme()`/`get_panel_css_vals()`/`parse_css_rgba()` — don't reintroduce per-view dicts.
- Slash commands: search bar `/new /open /gm /google` (tab-complete, `/gm` uses `evdev.UInput`); AI chat `/new /delete /fork /retry /rollback /title /model /cd /summary keep=N /skill` (`/fork` copies system-prompt snapshot).
- System prompt: global `AISettingsStore().system_prompt` (`ai_settings.json`) snapshotted into `conv.system_prompt` on creation; later global edits don't affect existing convs. Tests: `tests/test_system_prompt.py`.
- `data-tool-call-id` coupling: regex in `ai_text_utils/markdown.py:_escape_tool_results` must allow arbitrary attrs on `<details class="tool-step-details">` — marker `# ponytail: _escape_tool_results pattern coupling`.
- DB `~/.local/share/opencode/opencode.db` (`timeout=5`, `WAL`): excludes archived + `title LIKE '%(@%subagent)%'` + missing dirs. Live = `pgrep -f opencode` + `/proc/<pid>/cmdline|cwd` scan. Status `live`/`recent`(<24h)/`closed`. `delete_session()` tries `opencode session delete <id>` then falls back to soft-delete (`time_archived`). `part` table query uses `INNER JOIN + MAX(time_created)` to cut 49k→100 rows.
- Image orphans: `_delete_orphan_images()` on `clipboard_store.py:_load()` cleans unreferenced `images/` PNGs.

## Config & Cache Paths

| Path | Contents |
|------|----------|
| `~/.config/opencode-switcher/config.json` | Theme |
| `~/.config/opencode-switcher/clipboard_history.json` (+`.backup`) | 150 FIFO items |
| `~/.config/opencode-switcher/{categories,custom_prompts,llm_settings,ai_settings,agent_memory,mcp_servers}.json` + `todos.json`, `skills/`, `gmail_credentials/`, `qq_mail_credentials.json` | App state (LLM/MCP creds `0o600`) |
| `~/.config/opencode-switcher/lock` | Flock lock |
| `~/.cache/opencode-switcher/conversations/` | AI conversation JSON |
| `~/.cache/opencode-switcher/{toggle.sock,clipboard.updated,focus.request,last_written_hash}` | Wayland IPC |

## Conventions

- Strings: double quotes; docstrings `"""`; imports stdlib→third-party→local with `gi.require_version()` before `from gi.repository import`.
- Comments: `# <text>` (EN or ZH); `# ponytail:` marks intentionally removed code.
- Extension JS errors: `console.error('opencode-switcher: ...')`.
- Settings dialog factory: `show_settings_dialog(parent, on_dialog_shown, on_dialog_hidden)`.
- Before merge to `master`: run `codegraph sync` to refresh `.codegraph/` index (symlink to `~/.omo/codegraph/`).
- No formatter/linter — validate with `unittest`.

## References

- `gnome-extension/AGENTS.md` — extension internals
- `docs/usage.md` — user-facing feature/UX details
- `docs/plans/` + `.hzb-agents/experience/` + `.omo/plans/` — postmortems & plans (gitignored)
