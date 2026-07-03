# gnome-extension — Agent Instructions

GNOME Shell extension for Wayland clipboard monitoring (owner-changed signal) and window focus (file-based IPC). Bridges the gap for the opencode-switcher tray app on Wayland where X11 polling/focus tools don't work.

## STRUCTURE

```
gnome-extension/
├── extension.js    # Main extension logic (~350 lines)
└── metadata.json   # UUID + shell-version [48,49,50]
```

## WHERE TO LOOK

| Task | File | Notes |
|------|------|-------|
| Clipboard capture | `extension.js` class `ClipboardMonitor.enable()` | Connects `owner-changed` on `global.display.get_selection()`, filters by `selectionType === 1` (CLIPBOARD) |
| Append to history | `extension.js` `appendToHistory()` | Reads/writes `clipboard_history.json`, dedup by hash, writes `clipboard.updated` marker |
| Window focus | `extension.js` `setupFocusMonitor()` | `Gio.FileMonitor` on `focus.request`, calls `win.activate()` |
| Classification | `extension.js` `classifyText()` | Duplicates `clipboard_store.py` `classify_text()` — must keep in sync manually |
| Image notification | `extension.js` `notifyImage()` | Writes `clipboard.updated` with `image:` prefix (no image data in JS) |
| Own-write skip | `extension.js` lines 313-316 | Reads `last_written_hash`, skips if hash matches |

## CONVENTIONS

- **JS style**: GNOME Shell 45+ module system (`import` from `gi://`, `export default class`)
- **No linter/formatter**: Same as parent project. Manual discipline.
- **`const` for imports**: `const CONFIG_DIR = GLib.get_home_dir() + '/.config/...'`
- **Error handling**: `try/catch` with `console.error('opencode-switcher: ...')` prefix on every operation
- **Cleanup**: `disable()` disconnects signal + cancels file monitor

## ANTI-PATTERNS

- **Duplicated classification**: `classifyText()` in JS mirrors `clipboard_store.py` `classify_text()`. ~150 lines of heuristic scoring that drift independently. Both must be updated for any classification change.
- **Shared file race**: `clipboard_history.json` written by both this extension and `clipboard_panel.py`. Concurrent writes corrupt the JSON. `appendToHistory()` does read-modify-write with no locking.
- **Lossy marker IPC**: `clipboard.updated` is a single file with a timestamp. Rapid clipboard events can overwrite before the Python side reads it. Not a queue.
- **No Wayland image capture**: Image clipboard only writes a marker. The actual PNG is captured on the Python side via polling fallback. Possible race if image leaves clipboard before Python polls.
- **`St.Clipboard.get_text()` async gap**: The `owner-changed` fires before the clipboard content is available. The async callback may miss rapid successive copies.
