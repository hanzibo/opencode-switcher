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
        let ch = new GLib.Checksum(GLib.ChecksumType.SHA256);
        ch.update(text);
        return ch.get_string().slice(0, 16);
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

function classifyText(text) {
    let stripped = text.trim();
    if (!stripped) {
        return "text";
    }
    if (stripped.startsWith("http")) {
        return "link";
    }

    // 1. JSON Check
    try {
        let parsed = JSON.parse(stripped);
        if (parsed && typeof parsed === "object" && stripped.length > 4) {
            return "code";
        }
    } catch (e) {}

    // 2. HTML/XML Check
    if (/^\s*<(html|head|body|div|span|p|a|ul|ol|li|table|tr|td|script|style|link|meta|xml)\b/i.test(stripped)) {
        return "code";
    }
    if (/<\/?[a-zA-Z][a-zA-Z0-9]*>/.test(stripped) && stripped.includes("<") && stripped.includes(">")) {
        return "code";
    }

    // 3. Shebang / CLI Check
    if (/^#!\s*\/(bin|usr)\//.test(stripped)) {
        return "code";
    }
    if (/^\s*(sudo\s+)?(apt-get|yum|docker|systemctl|pip install|npm install|git clone|yarn add|pnpm add|chmod\s+\+x)\b/.test(stripped)) {
        return "code";
    }

    // 3.5 Curly Braces with Newline Check (typical of block-structured code)
    if (/[\{\}]\s*[\r\n]|[\r\n]\s*[\{\}]/.test(text)) {
        return "code";
    }

    // 4. Code Heuristics Scorer
    let score = 0;

    // Python
    if (/^\s*def\s+[a-zA-Z_]\w*\s*\(.*\)\s*:/m.test(stripped)) {
        score += 5;
    }
    if (/^\s*class\s+[a-zA-Z_]\w*(\s*\(.*\))?\s*:/m.test(stripped)) {
        score += 4;
    }
    if (stripped.includes("if __name__ ==")) {
        score += 5;
    }
    if (/^\s*(import\s+\w+|from\s+\w+\s+import)\b/m.test(stripped)) {
        score += 3;
    }

    // C/C++
    if (/^\s*#include\s*[<" ]/m.test(stripped)) {
        score += 5;
    }
    if (/^\s*#define\s+[a-zA-Z_]\w*/m.test(stripped)) {
        score += 3;
    }
    if (stripped.includes("std::cout") || stripped.includes("std::endl")) {
        score += 4;
    }
    if (/\busing namespace std\b/.test(stripped)) {
        score += 5;
    }

    // Java/C#/C++
    if (/\b(public|private|protected)\s+(class|interface|void|int|double|float|char|bool|boolean|string)\b/.test(stripped)) {
        score += 5;
    }
    if (stripped.includes("System.out.print")) {
        score += 4;
    }

    // JS/TS/Go/Rust
    if (/\bconsole\.log\(/.test(stripped)) {
        score += 4;
    }
    if (/\b(const|let|var)\s+[a-zA-Z_]\w*\s*=/.test(stripped)) {
        score += 3;
    }
    if (/\b(export|export default)\b/.test(stripped)) {
        score += 2;
    }
    if (/\bfunction\b/.test(stripped)) {
        score += 3;
    }
    if (/\b(fn|pub|struct|impl|package)\b/.test(stripped)) {
        score += 2;
    }

    // C-style comments
    if (/^\s*(\/\/|\/\*).*$/m.test(stripped)) {
        score += 3;
    }

    // SQL
    if (/\bSELECT\s+[\w\s,*()\-]+FROM\b/i.test(stripped)) {
        score += 5;
    }
    if (/\b(INSERT\s+INTO|UPDATE\s+\w+\s+SET|DELETE\s+FROM)\b/i.test(stripped)) {
        score += 5;
    }

    // Shell / Bash heuristics
    let bashAssignments = stripped.match(/^\s*[a-zA-Z_]\w*=[^\s=]+/gm) || [];
    score += Math.min(5, bashAssignments.length * 3);
    if (/\$\([^)]+\)/.test(stripped)) {
        score += 3;
    }
    if (/\[\[?\s+.*?\s+\]\]?/.test(stripped)) {
        score += 3;
    }
    if (/^\s*(fi|done|esac)\b/m.test(stripped)) {
        score += 3;
    }
    if (/^\s*for\s+[a-zA-Z_]\w*\s+in\b/m.test(stripped)) {
        score += 2;
    }

    // Generic specific keywords
    let keywords = [
        /\binterface\b/, /\bvoid\b/, /\bprintf\b/, /\bprintln\b/,
        /\bnullptr\b/, /\bundefined\b/, /\btypeof\b/, /\belseif\b/,
        /\belif\b/, /\bsizeof\b/, /\bstruct\b/, /\benum\b/, /\bstatic\b/,
        /\bclass\b/
    ];
    for (let kw of keywords) {
        if (kw.test(stripped)) {
            score += 2;
        }
    }

    // Line ending semicolons (excluding HTML entities)
    let semicolonMatches = stripped.match(/(?<!\&[a-zA-Z0-9]{2,6});\s*$/gm) || [];
    score += Math.min(3, semicolonMatches.length);

    // Curly braces match
    let leftBraces = (stripped.match(/\{/g) || []).length;
    let rightBraces = (stripped.match(/\}/g) || []).length;
    if (leftBraces > 0 && leftBraces === rightBraces) {
        score += Math.min(3, leftBraces);
    }

    if (score >= 4) {
        return "code";
    }
    return "text";
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

    let itemType = classifyText(text);
    items.push({ text, timestamp: Date.now(), hash, type: itemType, image_path: null });
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
                    if (mimetypes.includes('x-kde-passwordManagerHint')) {
                        return;
                    }
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
