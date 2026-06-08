import St from 'gi://St';
import GLib from 'gi://GLib';
import Gio from 'gi://Gio';

const CONFIG_DIR = GLib.get_home_dir() + '/.config/opencode-switcher';
const CACHE_DIR = GLib.get_home_dir() + '/.cache/opencode-switcher';
const HISTORY_PATH = CONFIG_DIR + '/clipboard_history.json';
const MARKER_PATH = CACHE_DIR + '/clipboard.updated';
const IGNORE_HASH_PATH = CACHE_DIR + '/last_written_hash';
const FOCUS_REQUEST_PATH = CACHE_DIR + '/focus.request';
const MAX_ITEMS = 150;

let ownerChangedId = 0;
let lastText = '';
let focusMonitor = null;

function getTextHash(text) {
    try {
        let enc = new TextEncoder().encode(text);
        let digest = GLib.sha256(enc) || [];
        return Array.from(new Uint8Array(digest), b => b.toString(16).padStart(2, '0')).join('').slice(0, 16);
    } catch (e) {
        return String(text.length) + '_' + String(Date.now());
    }
}

function getIgnoreHash() {
    try {
        let file = Gio.File.new_for_path(IGNORE_HASH_PATH);
        let [ok, bytes] = file.load_contents(null);
        if (ok && bytes instanceof Uint8Array) {
            return new TextDecoder().decode(bytes).trim();
        }
    } catch (e) {
        // file might not exist, which is fine
    }
    return '';
}

function appendToHistory(text, precomputedHash) {
    let items = [];
    try {
        let file = Gio.File.new_for_path(HISTORY_PATH);
        let [ok, bytes] = file.load_contents(null);
        if (ok && bytes instanceof Uint8Array) {
            items = JSON.parse(new TextDecoder().decode(bytes));
        }
    } catch (e) {
        items = [];
    }
    if (!Array.isArray(items)) items = [];

    let hash = precomputedHash ? precomputedHash : getTextHash(text);

    if (items.length > 0 && items[items.length - 1].hash === hash) return;

    items.push({ text, timestamp: Date.now(), hash, type: "text", image_path: null });
    if (items.length > MAX_ITEMS) items = items.slice(-MAX_ITEMS);

    try {
        GLib.mkdir_with_parents(CONFIG_DIR, 0o755);
        GLib.file_set_contents(HISTORY_PATH, JSON.stringify(items));
    } catch (e) {
        console.error('opencode-switcher: history write error: ' + e);
    }
    try {
        GLib.mkdir_with_parents(CACHE_DIR, 0o755);
        GLib.file_set_contents(MARKER_PATH, 'text:' + String(Date.now()));
    } catch (e) {
        console.error('opencode-switcher: marker write error: ' + e);
    }
}

function notifyImage() {
    try {
        GLib.mkdir_with_parents(CACHE_DIR, 0o755);
        GLib.file_set_contents(MARKER_PATH, 'image:' + String(Date.now()));
    } catch (e) {
        console.error('opencode-switcher: marker write error: ' + e);
    }
}

function focusWindow(query) {
    try {
        let queryLower = query.toLowerCase();
        let windows = global.display.list_all_windows() || [];
        for (let win of windows) {
            let wmClass = (win.get_wm_class() || "").toLowerCase();
            let title = (win.get_title() || "").toLowerCase();
            if (wmClass.includes(queryLower) || title.includes(queryLower)) {
                win.activate(global.get_current_time());
                return true;
            }
        }
    } catch (e) {
        console.error('opencode-switcher: focusWindow error: ' + e);
    }
    return false;
}

let isInternalWrite = false;

function setupFocusMonitor() {
    try {
        let file = Gio.File.new_for_path(FOCUS_REQUEST_PATH);
        GLib.mkdir_with_parents(CACHE_DIR, 0o755);
        if (!file.query_exists(null)) {
            GLib.file_set_contents(FOCUS_REQUEST_PATH, '');
        }

        focusMonitor = file.monitor_file(Gio.FileMonitorFlags.NONE, null);
        focusMonitor.connect('changed', (mon, fileObj, otherFile, eventType) => {
            if (eventType === Gio.FileMonitorEvent.CHANGES_DONE_HINT || eventType === Gio.FileMonitorEvent.CHANGED) {
                if (isInternalWrite) {
                    isInternalWrite = false;
                    return;
                }
                try {
                    let [ok, bytes] = file.load_contents(null);
                    if (ok && bytes instanceof Uint8Array) {
                        let content = new TextDecoder().decode(bytes).trim();
                        if (content) {
                            focusWindow(content);
                            isInternalWrite = true;
                            GLib.file_set_contents(FOCUS_REQUEST_PATH, '');
                        }
                    }
                } catch (e) {
                    console.error('opencode-switcher: focus read error: ' + e);
                    isInternalWrite = false;
                }
            }
        });
    } catch (e) {
        console.error('opencode-switcher: focus monitor setup error: ' + e);
    }
}


export default class ClipboardMonitor {
    enable() {
        lastText = '';
        let selection = global.display.get_selection();
        ownerChangedId = selection.connect('owner-changed', (sel, selectionType) => {
            if (selectionType === 1) { // Meta.SelectionType.SELECTION_CLIPBOARD
                try {
                    let mimetypes = sel.get_mimetypes(1) || [];
                    if (mimetypes.includes('image/png')) {
                        notifyImage();
                    } else if (mimetypes.length > 0) {
                        let clipboard = St.Clipboard.get_default();
                        clipboard.get_text(St.ClipboardType.CLIPBOARD, (clip, text) => {
                            try {
                                if (text) {
                                    let hash = getTextHash(text);
                                    let ignoreHash = getIgnoreHash();
                                    if (hash === ignoreHash) {
                                        lastText = text; // sync lastText
                                        return;
                                    }
                                    if (text !== lastText) {
                                        lastText = text;
                                        appendToHistory(text, hash);
                                    }
                                }
                            } catch (e) {
                                console.error('opencode-switcher: callback error: ' + e);
                            }
                        });
                    }
                } catch (e) {
                    console.error('opencode-switcher: owner-changed error: ' + e);
                }
            }
        });
        // Initial sync
        notifyImage();

        // Start watching for window focus requests
        setupFocusMonitor();
    }
    disable() {
        if (ownerChangedId) {
            let selection = global.display.get_selection();
            selection.disconnect(ownerChangedId);
            ownerChangedId = 0;
        }
        if (focusMonitor) {
            focusMonitor.cancel();
            focusMonitor = null;
        }
    }
}
