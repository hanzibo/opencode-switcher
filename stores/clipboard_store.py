import json
import re
import os
import sys
import threading
import copy
# jieba / rank_bm25 在 MemStore 中懒加载，非必须依赖

# Heuristic Code Classification Regexes
HTML_START_RE = re.compile(r'^\s*<(html|head|body|div|span|p|a|ul|ol|li|table|tr|td|script|style|link|meta|xml)\b', re.IGNORECASE)
HTML_END_RE = re.compile(r'</[a-zA-Z][a-zA-Z0-9]*>')
SHEBANG_RE = re.compile(r'^#!\s*/(bin|usr)/')
CLI_CMD_RE = re.compile(r'^\s*(sudo\s+)?(apt-get|yum|docker|systemctl|pip install|npm install|git clone|yarn add|pnpm add|chmod\s+\+x)\b')
CURLY_NEWLINE_RE = re.compile(r'[\{\}]\s*[\r\n]|[\r\n]\s*[\{\}]')
BASH_VAR_ASSIGN_RE = re.compile(r'^\s*[a-zA-Z_]\w*=[^\s=]+', re.MULTILINE)
BASH_CMD_SUBST_RE = re.compile(r'\$\([^)]+\)')
BASH_COND_RE = re.compile(r'\[\[?\s+.*?\s+\]\]?')
BASH_KEYWORD_RE = re.compile(r'^\s*(fi|done|esac)\b', re.MULTILINE)
BASH_LOOP_RE = re.compile(r'^\s*for\s+[a-zA-Z_]\w*\s+in\b', re.MULTILINE)

PY_DEF_RE = re.compile(r'^\s*def\s+[a-zA-Z_]\w*\s*\(.*\)\s*:', re.MULTILINE)
PY_CLASS_RE = re.compile(r'^\s*class\s+[a-zA-Z_]\w*(\s*\(.*\))?\s*:', re.MULTILINE)
PY_IMPORT_RE = re.compile(r'^\s*(import\s+\w+|from\s+\w+\s+import)\b', re.MULTILINE)

CPP_INCLUDE_RE = re.compile(r'^\s*#include\s*[<" ]', re.MULTILINE)
CPP_DEFINE_RE = re.compile(r'^\s*#define\s+[a-zA-Z_]\w*', re.MULTILINE)
CPP_USING_RE = re.compile(r'\busing namespace std\b')

TYPED_DECL_RE = re.compile(r'\b(public|private|protected)\s+(class|interface|void|int|double|float|char|bool|boolean|string)\b')

JS_CONSOLE_RE = re.compile(r'\bconsole\.log\(')
JS_VAR_RE = re.compile(r'\b(const|let|var)\s+[a-zA-Z_]\w*\s*=')
JS_FUNC_RE = re.compile(r'\b(export|export default)\b')
JS_FUNCTION_KW_RE = re.compile(r'\bfunction\b')
LANG_KEYWORDS_RE = re.compile(r'\b(fn|pub|struct|impl|package)\b')

C_COMMENT_RE = re.compile(r'^\s*(\/\/|\/\*).*$', re.MULTILINE)

SQL_SELECT_RE = re.compile(r'\bSELECT\s+[\w\s,*()\-]+FROM\b', re.IGNORECASE)
SQL_MOD_RE = re.compile(r'\b(INSERT\s+INTO|UPDATE\s+\w+\s+SET|DELETE\s+FROM)\b', re.IGNORECASE)

GENERIC_KW_RE = re.compile(r'\b(interface|void|printf|println|nullptr|undefined|typeof|elseif|elif|sizeof|struct|enum|static|class)\b')

SEMICOLON_RE = re.compile(r'(?<!&\w)(?<!&#\d);\s*$', re.MULTILINE)
CURLY_BRACE_RE = re.compile(r'[\{\}]')
import subprocess
import time
import hashlib
from copy import deepcopy
from dataclasses import dataclass, asdict
from typing import Optional, List, Dict, Any, Union
from uuid import uuid4

from system.utils import CONVERSATIONS_DIR, write_json_private

CONFIG_DIR = os.path.expanduser("~/.config/opencode-switcher")
CLIPBOARD_PATH = os.path.join(CONFIG_DIR, "clipboard_history.json")
# ponytail: removed unused Prompt, PromptStore, and migrate_from_prompts, MAX_CLIPBOARD
CATEGORIES_PATH = os.path.join(CONFIG_DIR, "categories.json")
CUSTOM_PROMPTS_PATH = os.path.join(CONFIG_DIR, "custom_prompts.json")



def _content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


@dataclass
class ClipboardItem:
    text: str
    timestamp: int
    hash: str
    type: str = "text"
    image_path: Optional[str] = None
    language: Optional[str] = None



@dataclass
class CategoryItem:
    title: str
    text: str
    timestamp: int


@dataclass
class CustomCategory:
    id: str
    name: str
    items: List[CategoryItem]
    pinned: bool = False
    created_at: int = 0


def classify_text(text: str) -> str:
    stripped = text.strip()
    if not stripped:
        return "text"
        
    if stripped.startswith("http"):
        return "link"
        
    # 1. JSON Check
    try:
        parsed = json.loads(stripped)
        if isinstance(parsed, (dict, list)) and len(stripped) > 4:
            return "code"
    except Exception:
        pass

    # 2. HTML/XML Check
    if HTML_START_RE.match(stripped):
        return "code"
    if HTML_END_RE.search(stripped):
        return "code"

    # 3. Shebang / CLI Check
    if SHEBANG_RE.match(stripped):
        return "code"
    if CLI_CMD_RE.match(stripped):
        return "code"

    # 3.5 Curly Braces with Newline Check (typical of block-structured code)
    if CURLY_NEWLINE_RE.search(text):
        return "code"

    # 4. Code Heuristics Scorer
    score = 0

    # Python
    if PY_DEF_RE.search(stripped):
        score += 5
    if PY_CLASS_RE.search(stripped):
        score += 4
    if "if __name__ ==" in stripped:
        score += 5
    if PY_IMPORT_RE.search(stripped):
        score += 3

    # C/C++
    if CPP_INCLUDE_RE.search(stripped):
        score += 5
    if CPP_DEFINE_RE.search(stripped):
        score += 3
    if "std::cout" in stripped or "std::endl" in stripped:
        score += 4
    if CPP_USING_RE.search(stripped):
        score += 5

    # Java/C#/C++
    if TYPED_DECL_RE.search(stripped):
        score += 5
    if "System.out.print" in stripped:
        score += 4

    # JS/TS/Go/Rust
    if JS_CONSOLE_RE.search(stripped):
        score += 4
    if JS_VAR_RE.search(stripped):
        score += 3
    if JS_FUNC_RE.search(stripped):
        score += 2
    if JS_FUNCTION_KW_RE.search(stripped):
        score += 3
    if LANG_KEYWORDS_RE.search(stripped):
        score += 2

    # C-style comments
    if C_COMMENT_RE.search(stripped):
        score += 3

    # SQL
    if SQL_SELECT_RE.search(stripped):
        score += 5
    if SQL_MOD_RE.search(stripped):
        score += 5

    # Shell / Bash
    bash_assignments = BASH_VAR_ASSIGN_RE.findall(stripped)
    score += min(5, len(bash_assignments) * 3)
    if BASH_CMD_SUBST_RE.search(stripped):
        score += 3
    if BASH_COND_RE.search(stripped):
        score += 3
    if BASH_KEYWORD_RE.search(stripped):
        score += 3
    if BASH_LOOP_RE.search(stripped):
        score += 2

    # Generic specific keywords
    if GENERIC_KW_RE.search(stripped):
        score += 2

    # Line ending semicolons
    semicolons = SEMICOLON_RE.findall(stripped)
    score += min(3, len(semicolons))

    # Curly braces match
    left_braces = stripped.count('{')
    right_braces = stripped.count('}')
    if left_braces > 0 and left_braces == right_braces:
        score += min(3, left_braces)

    if score >= 4:
        return "code"
    return "text"

def detect_language_name(text: str) -> Optional[str]:
    stripped = text.strip()
    if not stripped:
        return None
        
    # 1. JSON Check
    try:
        parsed = json.loads(stripped)
        if isinstance(parsed, (dict, list)) and len(stripped) > 4:
            return "JSON"
    except Exception:
        pass

    # 2. HTML/XML Check
    if HTML_START_RE.match(stripped) or HTML_END_RE.search(stripped):
        return "HTML"

    # 3. Shebang / CLI Check
    if SHEBANG_RE.match(stripped):
        line = stripped.splitlines()[0]
        if "python" in line:
            return "Python"
        if "bash" in line or "sh" in line:
            return "Shell"
        if "node" in line or "js" in line:
            return "JavaScript"
        return "Shell"
        
    if CLI_CMD_RE.match(stripped):
        return "Shell"

    # 4. Code Heuristics Scorer - check scores for each language
    python_score = 0
    if PY_DEF_RE.search(stripped): python_score += 5
    if PY_CLASS_RE.search(stripped): python_score += 4
    if "if __name__ ==" in stripped: python_score += 5
    if PY_IMPORT_RE.search(stripped): python_score += 3

    cpp_score = 0
    if CPP_INCLUDE_RE.search(stripped): cpp_score += 5
    if CPP_DEFINE_RE.search(stripped): cpp_score += 3
    if "std::cout" in stripped or "std::endl" in stripped: cpp_score += 4
    if CPP_USING_RE.search(stripped): cpp_score += 5

    java_score = 0
    if TYPED_DECL_RE.search(stripped): java_score += 5
    if "System.out.print" in stripped: java_score += 4

    js_score = 0
    if JS_CONSOLE_RE.search(stripped): js_score += 4
    if JS_VAR_RE.search(stripped): js_score += 3
    if JS_FUNC_RE.search(stripped): js_score += 2
    if JS_FUNCTION_KW_RE.search(stripped): js_score += 3

    rust_go_score = 0
    if LANG_KEYWORDS_RE.search(stripped): rust_go_score += 2

    sql_score = 0
    if SQL_SELECT_RE.search(stripped): sql_score += 5
    if SQL_MOD_RE.search(stripped): sql_score += 5

    bash_score = 0
    bash_assignments = BASH_VAR_ASSIGN_RE.findall(stripped)
    bash_score += min(5, len(bash_assignments) * 3)
    if BASH_CMD_SUBST_RE.search(stripped): bash_score += 3
    if BASH_COND_RE.search(stripped): bash_score += 3
    if BASH_KEYWORD_RE.search(stripped): bash_score += 3
    if BASH_LOOP_RE.search(stripped): bash_score += 2

    scores = {
        "Python": python_score,
        "C++": cpp_score,
        "Java": java_score,
        "JavaScript": js_score,
        "Rust/Go": rust_go_score,
        "SQL": sql_score,
        "Shell": bash_score
    }
    
    max_lang, max_score = max(scores.items(), key=lambda x: x[1])
    if max_score > 0:
        return max_lang
        
    if CURLY_BRACE_RE.search(stripped) and SEMICOLON_RE.search(stripped):
        return "C/JS"
        
    return "Code"

class ClipboardStore:
    def __init__(self):
        self._lock = threading.RLock()
        self._items: List[ClipboardItem] = []
        self._last_written_hash: Optional[str] = None
        self._max_clipboard: int = AISettingsStore().max_clipboard
        with self._lock:
            self._load()

    def _read_items(self, path) -> Optional[List[ClipboardItem]]:
        """Parse a history file into ClipboardItems, or None if missing/corrupt."""
        if not os.path.isfile(path):
            return None
        try:
            with open(path, "r") as f:
                data = json.load(f)
            items = []
            for d in data[-self._max_clipboard:]:
                if isinstance(d, dict) and "type" not in d:
                    d["type"] = "image" if d.get("image_path") else classify_text(d.get("text") or "")
                item = ClipboardItem(**d)
                if not getattr(item, "type", None):
                    item.type = "image" if getattr(item, "image_path", None) else classify_text(item.text or "")
                items.append(item)
            return items
        except (json.JSONDecodeError, TypeError):
            return None

    def _load(self):
        with self._lock:
            items = self._read_items(CLIPBOARD_PATH)
            if items is not None:
                # Valid primary: leave it untouched (never overwrite a valid file).
                self._items = items
            else:
                # Primary is missing or corrupt — try the backup before giving up,
                # so valid history is not silently lost.
                backup = self._read_items(CLIPBOARD_PATH + ".backup")
                if backup is not None:
                    self._items = backup
                    # Heal: atomically rewrite the primary from the recovered
                    # data. Only reached when the primary was invalid.
                    self._write_items_to(CLIPBOARD_PATH, backup)
                # else: both missing/corrupt — keep the existing in-memory state
                # (empty on startup, matching the previous empty-history fallback).
            self._delete_orphan_images()

    @staticmethod
    def _fsync_dir(path):
        """Best-effort fsync of the containing directory to persist the rename."""
        pass

    @staticmethod
    def _write_items_to(path, items):
        """Atomically write items to path via a same-directory temp file.

        Readers never observe a partially-written file: the temp file is fully
        written, flushed and fsynced before os.replace() swaps it into place.
        On any failure the temp file is removed so no partial state lingers.
        The temp file is created with mode 0o600, and os.replace() preserves
        that mode, so clipboard history (and its backup) is user-private.
        """
        tmp_path = path + ".tmp"
        try:
            flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
            fd = os.open(tmp_path, flags, 0o600)
            with os.fdopen(fd, "w") as f:
                json.dump([asdict(i) for i in items], f)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, path)
            ClipboardStore._fsync_dir(path)
        finally:
            # Clean up the temp file on failure (or no-op after a successful replace).
            try:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
            except OSError:
                pass

    def _delete_orphan_images(self):
        img_dir = os.path.join(CONFIG_DIR, "images")
        if not os.path.isdir(img_dir):
            return
        referenced = {item.image_path for item in self._items if item.image_path}
        for fname in os.listdir(img_dir):
            fpath = os.path.join(img_dir, fname)
            if fpath not in referenced and os.path.isfile(fpath):
                try:
                    os.remove(fpath)
                except Exception as e:
                    sys.stderr.write(f"Error deleting orphan image {fpath}: {e}\n")
                    sys.stderr.flush()

    def _save(self):
        with self._lock:
            os.makedirs(CONFIG_DIR, exist_ok=True)
            self._write_items_to(CLIPBOARD_PATH, self._items)
            # Keep a copy of the freshly written state as the backup so a
            # later corrupt/missing primary can be fully recovered. Best-effort:
            # the primary write above is already complete and durable.
            try:
                self._write_items_to(CLIPBOARD_PATH + ".backup", self._items)
            except Exception:
                pass

    def classify_text(self, text: str) -> str:
        return classify_text(text)

    def detect_language_name(self, text: str) -> Optional[str]:
        return detect_language_name(text)

    def _delete_image_file_if_unreferenced(self, image_path: str):
        if not image_path:
            return
        # Count references to this image path in the current items list
        ref_count = sum(1 for item in self._items if item.image_path == image_path)
        if ref_count == 0:
            try:
                if os.path.exists(image_path):
                    os.remove(image_path)
            except Exception as e:
                sys.stderr.write(f"Error deleting image file {image_path}: {e}\n")
                sys.stderr.flush()

    def add(self, text: str):
        with self._lock:
            if not text.strip():
                return
            h = _content_hash(text)
            if h == self._last_written_hash:
                return
            if self._items and self._items[-1].hash == h:
                return
            item_type = self.classify_text(text)
            language = self.detect_language_name(text) if item_type == "code" else None
            self._items.append(ClipboardItem(text=text, timestamp=int(time.time() * 1000), hash=h, type=item_type, language=language))
            if len(self._items) > self._max_clipboard:
                evicted = self._items[:-self._max_clipboard]
                self._items = self._items[-self._max_clipboard:]
                for item in evicted:
                    if item.type == "image" and item.image_path:
                        self._delete_image_file_if_unreferenced(item.image_path)
            self._save()

    def add_image(self, image_data: bytes):
        with self._lock:
            if not image_data:
                return
            h = hashlib.sha256(image_data).hexdigest()[:16]
            if h == self._last_written_hash:
                return
            if self._items and self._items[-1].hash == h:
                return
            
            img_dir = os.path.join(CONFIG_DIR, "images")
            os.makedirs(img_dir, exist_ok=True)
            img_path = os.path.join(img_dir, f"{h}.png")
            
            if not os.path.exists(img_path):
                try:
                    with open(img_path, "wb") as f:
                        f.write(image_data)
                except Exception:
                    return
                    
            self._items.append(ClipboardItem(
                text="[Image]",
                timestamp=int(time.time() * 1000),
                hash=h,
                type="image",
                image_path=img_path
            ))
            if len(self._items) > self._max_clipboard:
                evicted = self._items[:-self._max_clipboard]
                self._items = self._items[-self._max_clipboard:]
                for item in evicted:
                    if item.type == "image" and item.image_path:
                        self._delete_image_file_if_unreferenced(item.image_path)
            self._save()
        
        # Notify UI by writing marker
        cache_dir = os.path.expanduser("~/.cache/opencode-switcher")
        marker_path = os.path.join(cache_dir, "clipboard.updated")
        try:
            os.makedirs(cache_dir, exist_ok=True)
            with open(marker_path, "w") as f:
                f.write(str(int(time.time() * 1000)))
        except Exception:
            pass

    def delete(self, index: int):
        with self._lock:
            if 0 <= index < len(self._items):
                item = self._items.pop(index)
                if item.type == "image" and item.image_path:
                    self._delete_image_file_if_unreferenced(item.image_path)
                self._save()

    def clear_all(self):
        with self._lock:
            old_items = list(self._items)
            self._items.clear()
            for item in old_items:
                if item.type == "image" and item.image_path:
                    self._delete_image_file_if_unreferenced(item.image_path)
            self._save()

    def get_all(self) -> List[ClipboardItem]:
        with self._lock:
            return list(self._items)

    def reload(self):
        with self._lock:
            self._load()

    def mark_written(self, text: str, item_hash: Optional[str] = None):
        with self._lock:
            h = item_hash if item_hash else _content_hash(text)
            self._last_written_hash = h
            # Write to cache file for the GNOME extension to read
            cache_dir = os.path.expanduser("~/.cache/opencode-switcher")
            hash_path = os.path.join(cache_dir, "last_written_hash")
            try:
                os.makedirs(cache_dir, exist_ok=True)
                with open(hash_path, "w") as f:
                    f.write(h)
            except Exception:
                pass



class CategoryStore:
    def __init__(self):
        self._categories: List[CustomCategory] = []
        self._recycle_bin: List[Dict[str, Any]] = []
        self._load()

    def _load(self):
        if not os.path.isfile(CATEGORIES_PATH):
            self._save()
            return
        try:
            with open(CATEGORIES_PATH) as f:
                data = json.load(f)
            self._categories = []
            for c in data.get("categories", []):
                items = [CategoryItem(**i) for i in c.get("items", [])]
                cat = CustomCategory(
                    id=c["id"], name=c["name"], items=items,
                    pinned=c.get("pinned", False), created_at=c.get("created_at", 0)
                )
                self._categories.append(cat)
            self._recycle_bin = data.get("recycle_bin", [])
        except (json.JSONDecodeError, TypeError, KeyError):
            self._categories = []
            self._recycle_bin = []

    def _save(self):
        os.makedirs(CONFIG_DIR, exist_ok=True)
        with open(CATEGORIES_PATH, "w") as f:
            json.dump({
                "version": 1,
                "categories": [asdict(c) for c in self._categories],
                "recycle_bin": self._recycle_bin
            }, f, indent=2)

    @staticmethod
    def _assert_not_clipboard(cat_id: str):
        if cat_id == "__clipboard__":
            raise ValueError("Cannot modify the Clipboard category")

    @staticmethod
    def _clipboard_category() -> CustomCategory:
        return CustomCategory(
            id="__clipboard__", name="Clipboard", items=[], pinned=True, created_at=0
        )

    def get_all(self) -> List[CustomCategory]:
        clipboard_cat = self._clipboard_category()
        pinned = [c for c in self._categories if c.pinned]
        unpinned = [c for c in self._categories if not c.pinned]
        return [clipboard_cat] + [deepcopy(c) for c in pinned] + [deepcopy(c) for c in unpinned]

    def get(self, cat_id: str) -> Optional[CustomCategory]:
        if cat_id == "__clipboard__":
            return self._clipboard_category()
        for c in self._categories:
            if c.id == cat_id:
                return deepcopy(c)
        return None

    def create(self, name: str) -> str:
        if not name.strip():
            raise ValueError("Category name cannot be empty")
        if any(c.name == name for c in self._categories):
            raise ValueError(f"Category '{name}' already exists")
        cat_id = uuid4().hex[:12]
        self._categories.insert(0, CustomCategory(
            id=cat_id, name=name, items=[], pinned=False,
            created_at=int(time.time() * 1000)
        ))
        self._save()
        return cat_id

    def reorder_categories(self, new_categories: List[CustomCategory]):
        orig_ids = {c.id for c in self._categories}
        new_ids = {c.id for c in new_categories}
        if orig_ids != new_ids:
            raise ValueError("Reordered categories do not match original categories")
        self._categories = list(new_categories)
        self._save()

    def delete(self, cat_id: str):
        self._assert_not_clipboard(cat_id)
        for c in self._categories:
            if c.id == cat_id:
                if c.pinned:
                    raise ValueError("Cannot delete a pinned category")
                self._categories.remove(c)
                self._save()
                return
        raise ValueError(f"Category '{cat_id}' not found")

    def rename(self, cat_id: str, new_name: str):
        self._assert_not_clipboard(cat_id)
        if not new_name.strip():
            raise ValueError("Category name cannot be empty")
        if any(c.name == new_name for c in self._categories if c.id != cat_id):
            raise ValueError(f"Category '{new_name}' already exists")
        for c in self._categories:
            if c.id == cat_id:
                c.name = new_name
                self._save()
                return
        raise ValueError(f"Category '{cat_id}' not found")

    def set_pinned(self, cat_id: str, pinned: bool):
        self._assert_not_clipboard(cat_id)
        for c in self._categories:
            if c.id == cat_id:
                if c.pinned == pinned:
                    return
                c.pinned = pinned
                self._save()
                return
        raise ValueError(f"Category '{cat_id}' not found")

    def add_item(self, cat_id: str, title: str, text: str):
        self._assert_not_clipboard(cat_id)
        for c in self._categories:
            if c.id == cat_id:
                c.items.append(CategoryItem(
                    title=title, text=text, timestamp=int(time.time() * 1000)
                ))
                self._save()
                return
        raise ValueError(f"Category '{cat_id}' not found")

    def update_item(self, cat_id: str, index: int, title: str, text: str):
        self._assert_not_clipboard(cat_id)
        for c in self._categories:
            if c.id == cat_id:
                if 0 <= index < len(c.items):
                    c.items[index] = CategoryItem(
                        title=title, text=text, timestamp=int(time.time() * 1000)
                    )
                    self._save()
                    return
                raise IndexError("Item index out of range")
        raise ValueError(f"Category '{cat_id}' not found")

    def delete_item(self, cat_id: str, index: int):
        self._assert_not_clipboard(cat_id)
        for c in self._categories:
            if c.id == cat_id:
                if 0 <= index < len(c.items):
                    item = c.items.pop(index)
                    self.add_to_recycle_bin(c.id, c.name, item)
                    return
                raise IndexError("Item index out of range")
        raise ValueError(f"Category '{cat_id}' not found")

    def get_recycle_bin(self) -> List[Dict[str, Any]]:
        return list(self._recycle_bin)

    def add_to_recycle_bin(self, original_cat_id: str, original_cat_name: str, item: CategoryItem):
        self._recycle_bin.append({
            "original_cat_id": original_cat_id,
            "original_cat_name": original_cat_name,
            "item": asdict(item),
            "deleted_at": int(time.time() * 1000)
        })
        self._save()

    def restore_item(self, index: int):
        if 0 <= index < len(self._recycle_bin):
            entry = self._recycle_bin.pop(index)
            orig_id = entry["original_cat_id"]
            orig_name = entry["original_cat_name"]
            item_data = entry["item"]
            item = CategoryItem(
                title=item_data["title"],
                text=item_data["text"],
                timestamp=item_data["timestamp"]
            )
            
            # Find if the original category still exists by id or name
            target_cat = None
            for c in self._categories:
                if c.id == orig_id:
                    target_cat = c
                    break
            if not target_cat:
                for c in self._categories:
                    if c.name == orig_name:
                        target_cat = c
                        break
            
            # If not found, create a new category
            if not target_cat:
                new_cat_id = self.create(orig_name)
                for c in self._categories:
                    if c.id == new_cat_id:
                        target_cat = c
                        break
            
            if target_cat:
                target_cat.items.append(item)
            self._save()
            return True
        return False

    def permanently_delete_item(self, index: int):
        if 0 <= index < len(self._recycle_bin):
            self._recycle_bin.pop(index)
            self._save()
            return True
        return False


    def reorder_items(self, cat_id: str, new_items: List[CategoryItem]):
        """Replace the items list of a category with a reordered copy."""
        self._assert_not_clipboard(cat_id)
        for c in self._categories:
            if c.id == cat_id:
                c.items = list(new_items)
                self._save()
                return
        raise ValueError(f"Category '{cat_id}' not found")


# ── AI Conversation Data Models ────────────────────────────────────────────────

@dataclass
class ChatMessage:
    role: str  # "user", "assistant", "system", "tool"
    content: Union[str, List[Dict]]  # str for text, list for multimodal (vision/audio)
    tool_call_id: Optional[str] = None  # for "tool" role: links to the tool call that produced this result
    name: Optional[str] = None          # for "tool" role: name of the tool that was called
    tool_calls: Optional[List[Dict]] = None  # for "assistant" role: tool_calls array from LLM
    reasoning_content: Optional[str] = None  # for "assistant" role: reasoning / thinking text


@dataclass
class Conversation:
    id: str
    title: str
    system_prompt: str
    messages: List[ChatMessage]
    model_config_snapshot: Dict[str, Any]  # alias, base_url, model_name, temperature, max_tokens, top_p
    created_at: int
    updated_at: int
    summary: str = ""  # 跨会话持久化的历史摘要，供摘要压缩功能使用


class ConversationStore:
    """Persists AI conversations as JSON files in ``CONVERSATIONS_DIR``.

    A compact metadata index (``.conversations.index`` inside the same
    directory) lets ``list_conversations()`` avoid parsing every full
    conversation JSON on each refresh. The index is maintained on every
    create/save/delete/fork, reconciled against the directory on each list
    (missing / unindexed / mtime-changed files), and rebuilt from a full
    scan when corrupt or absent.
    """

    _INDEX_FILENAME = ".conversations.index"

    def __init__(self):
        self._dir = CONVERSATIONS_DIR
        os.makedirs(self._dir, exist_ok=True)
        self._lock = threading.RLock()
        self._index: Optional[Dict[str, Dict[str, Any]]] = None

    @property
    def _index_path(self) -> str:
        # Property (not cached at init) so tests that redirect ``_dir``
        # automatically redirect the index file as well.
        return os.path.join(self._dir, self._INDEX_FILENAME)

    def _path(self, conv_id: str) -> str:
        return os.path.join(self._dir, f"{conv_id}.json")

    # ── Metadata index ──────────────────────────────────────────────────────

    def _load_index(self) -> Dict[str, Dict[str, Any]]:
        """Load the metadata index from disk (lazily). Caller holds ``_lock``.

        A missing or corrupt index file yields an empty index, which the next
        list_conversations() call rebuilds from a full scan.
        """
        if self._index is not None:
            return self._index
        index = {}
        try:
            with open(self._index_path) as f:
                data = json.load(f)
            raw = data.get("conversations") if isinstance(data, dict) else None
            if isinstance(raw, dict):
                for conv_id, entry in raw.items():
                    if not isinstance(entry, dict):
                        continue
                    try:
                        index[str(conv_id)] = {
                            "id": str(entry.get("id") or conv_id),
                            "title": str(entry.get("title") or "(untitled)"),
                            "summary": str(entry.get("summary") or ""),
                            "message_count": int(entry.get("message_count") or 0),
                            "updated_at": int(entry.get("updated_at") or 0),
                            "mtime": float(entry.get("mtime") or 0.0),
                        }
                    except (TypeError, ValueError):
                        # Per-entry corruption: skip it; the next list heals it
                        # by re-parsing the conversation file.
                        continue
        except (OSError, ValueError):
            # Missing or unparseable index file -> rebuilt from a full scan.
            index = {}
        self._index = index
        return index

    def _write_index(self) -> None:
        """Atomically persist the in-memory index. Caller holds ``_lock``.

        Same-dir temp file, flushed and fsynced before ``os.replace()`` swaps
        it into place (P0 pattern from ClipboardStore._write_items_to), so
        readers never observe a partially-written index.
        """
        if self._index is None:
            return
        tmp_path = self._index_path + ".tmp"
        try:
            flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
            fd = os.open(tmp_path, flags, 0o600)
            with os.fdopen(fd, "w") as f:
                json.dump({"version": 1, "conversations": self._index}, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, self._index_path)
        finally:
            # Clean up the temp file on failure (or no-op after a successful replace).
            try:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
            except OSError:
                pass

    def _set_index_entry(self, entry: Dict[str, Any]) -> None:
        """Insert/update one metadata entry in the index and persist it."""
        with self._lock:
            index = self._load_index()
            if index.get(entry["id"]) == entry:
                return  # identical entry: skip the redundant durable write
            index[entry["id"]] = entry
            try:
                self._write_index()
            except OSError:
                pass  # durable write failed; mtime reconciliation heals later

    def _drop_index_entry(self, conv_id: str) -> None:
        """Remove one metadata entry from the index and persist it."""
        with self._lock:
            index = self._load_index()
            if conv_id not in index:
                return
            del index[conv_id]
            try:
                self._write_index()
            except OSError:
                pass

    @staticmethod
    def _entry_from_conversation(conv: Conversation, mtime: float) -> Dict[str, Any]:
        """Build an index entry from a live Conversation object."""
        return {
            "id": conv.id,
            "title": conv.title or "(untitled)",
            "summary": conv.summary or "",
            "message_count": len(conv.messages),
            "updated_at": conv.updated_at,
            "mtime": mtime,
        }

    @staticmethod
    def _entry_from_data(data: Any, mtime: float) -> Optional[Dict[str, Any]]:
        """Build an index entry from a parsed conversation JSON dict (fallback)."""
        if not isinstance(data, dict):
            return None
        conv_id = data.get("id")
        if not isinstance(conv_id, str) or not conv_id:
            return None
        messages = data.get("messages")
        message_count = len(messages) if isinstance(messages, list) else 0
        try:
            updated_at = int(data.get("updated_at") or 0)
        except (TypeError, ValueError):
            updated_at = 0
        return {
            "id": conv_id,
            "title": data.get("title") or "(untitled)",
            "summary": data.get("summary") or "",
            "message_count": message_count,
            "updated_at": updated_at,
            "mtime": mtime,
        }

    @staticmethod
    def _parse_meta(path: str) -> Optional[Dict[str, Any]]:
        """Extract metadata from a single conversation JSON file."""
        try:
            with open(path) as f:
                data = json.load(f)
            return ConversationStore._entry_from_data(data, os.path.getmtime(path))
        except Exception:
            return None

    def _full_scan(self) -> Dict[str, Dict[str, Any]]:
        """Rebuild the whole index by parsing every conversation file.

        This is the original list_conversations() scan, kept as the fallback
        for missing/corrupt/unindexed states.
        """
        index = {}
        if not os.path.isdir(self._dir):
            return index
        for fname in sorted(os.listdir(self._dir), reverse=True):
            if not fname.endswith(".json"):
                continue
            entry = self._parse_meta(os.path.join(self._dir, fname))
            if entry is not None:
                index[entry["id"]] = entry
        return index

    def create_conversation(self, title: str = "", system_prompt: str = "",
                            model_config: Optional[Dict[str, str]] = None) -> Conversation:
        now = int(time.time() * 1000)
        conv = Conversation(
            id=uuid4().hex[:12],
            title=title or "New Conversation",
            system_prompt=system_prompt,
            messages=[],
            model_config_snapshot=model_config or {},
            created_at=now,
            updated_at=now,
        )
        self.save_conversation(conv)
        return conv

    def save_conversation(self, conv: Conversation, bump_updated_at: bool = True):
        if bump_updated_at:
            conv.updated_at = int(time.time() * 1000)
        path = self._path(conv.id)
        write_json_private(path, {
            "id": conv.id,
            "title": conv.title,
            "system_prompt": conv.system_prompt,
            "messages": [asdict(m) for m in conv.messages],
            "summary": conv.summary,
            "model_config_snapshot": conv.model_config_snapshot,
            "created_at": conv.created_at,
            "updated_at": conv.updated_at,
        }, indent=2)
        # Maintain the metadata index after a successful file write. Best-effort:
        # a failed index write is healed by mtime reconciliation on the next list.
        try:
            entry = self._entry_from_conversation(conv, os.path.getmtime(path))
            self._set_index_entry(entry)
        except OSError:
            pass

    def load_conversation(self, conv_id: str) -> Optional[Conversation]:
        path = self._path(conv_id)
        if not os.path.isfile(path):
            return None
        try:
            with open(path) as f:
                data = json.load(f)
            messages = [ChatMessage(**m) for m in data.get("messages", [])]
            return Conversation(
                id=data["id"],
                title=data.get("title", ""),
                system_prompt=data.get("system_prompt", ""),
                messages=messages,
                summary=data.get("summary", ""),
                model_config_snapshot=data.get("model_config_snapshot", {}),
                created_at=data.get("created_at", 0),
                updated_at=data.get("updated_at", 0),
            )
        except (json.JSONDecodeError, KeyError, TypeError):
            return None

    def delete_conversation(self, conv_id: str):
        # Kill the corresponding bash session before removing data
        from tool_registry import close_bash_session
        close_bash_session(conv_id)
        path = self._path(conv_id)
        try:
            if os.path.isfile(path):
                os.remove(path)
        except OSError:
            pass
        self._drop_index_entry(conv_id)

    def fork_conversation(self, src_conv_id: str, new_title: Optional[str] = None) -> Optional[Conversation]:
        """Create a complete copy of an existing conversation with a new conversation ID."""
        src_conv = self.load_conversation(src_conv_id)
        if not src_conv:
            return None
        now = int(time.time() * 1000)
        title = new_title if new_title else f"{src_conv.title or 'New Conversation'} (Fork)"
        cloned_messages = [ChatMessage(**asdict(m)) for m in src_conv.messages]
        new_conv = Conversation(
            id=uuid4().hex[:12],
            title=title,
            system_prompt=src_conv.system_prompt,
            messages=cloned_messages,
            summary=src_conv.summary,
            model_config_snapshot=copy.deepcopy(src_conv.model_config_snapshot) if src_conv.model_config_snapshot else {},
            created_at=now,
            updated_at=now,
        )
        try:
            self.save_conversation(new_conv, bump_updated_at=False)
            return new_conv
        except Exception as e:
            print(f"Error saving forked conversation: {e}", flush=True)
            return None

    def list_conversations(self) -> List[Dict[str, Any]]:
        """List conversation metadata, served from the durable index.

        Steady state (warm index): one directory listing + one stat per
        conversation file, no JSON parsing. Files that are missing, unindexed
        or modified on disk since the last index write are reconciled
        individually; a corrupt/missing index triggers a full-scan rebuild.
        Returns the same {"id","title","summary","message_count","updated_at"}
        dicts as before, ordered by updated_at descending.
        """
        if not os.path.isdir(self._dir):
            return []
        with self._lock:
            index = self._load_index()
            conv_files = {}
            for fname in os.listdir(self._dir):
                if not fname.endswith(".json"):
                    continue
                path = os.path.join(self._dir, fname)
                if os.path.isfile(path):
                    conv_files[fname[:-5]] = path
            file_ids = set(conv_files)
            changed = False

            # Drop index entries whose conversation file no longer exists —
            # never return stale metadata for a missing conversation.
            for conv_id in list(index):
                if conv_id not in file_ids:
                    del index[conv_id]
                    changed = True

            # Re-read files that are unindexed (external create) or modified
            # since the last index write (external edit / crash between file
            # and index writes). stat-only in the common case.
            stale = set()
            for conv_id, path in conv_files.items():
                try:
                    mtime = os.path.getmtime(path)
                except OSError:
                    continue
                entry = index.get(conv_id)
                if entry is None or abs((entry.get("mtime") or 0.0) - mtime) > 1e-6:
                    stale.add(conv_id)
            if stale:
                for conv_id in stale:
                    entry = self._parse_meta(conv_files[conv_id])
                    if entry is not None:
                        index[conv_id] = entry
                    else:
                        index.pop(conv_id, None)
                changed = True

            # Index unusable (corrupt/missing) but conversations exist:
            # fall back to the full scan and rebuild the index.
            if not index and conv_files:
                index = self._full_scan()
                self._index = index
                changed = True

            if changed:
                try:
                    self._write_index()
                except OSError:
                    pass

            entries = sorted(index.values(),
                             key=lambda e: e.get("updated_at", 0), reverse=True)
            return [
                {k: e[k] for k in ("id", "title", "summary", "message_count", "updated_at")}
                for e in entries
            ]


# ── Clipboard capture ─────────────────────────────────────────────────────────

def _capture_image() -> Optional[bytes]:
    try:
        types = subprocess.check_output(["wl-paste", "--list-types"], stderr=subprocess.DEVNULL, timeout=0.5).decode("utf-8", errors="ignore")
        if "image/png" in types:
            res = subprocess.run(["wl-paste", "--type", "image/png"], capture_output=True, timeout=1)
            if res.returncode == 0 and res.stdout:
                return res.stdout
    except Exception:
        pass
    return None


def _clipboard_cmd() -> list:
    return ["wl-paste"]


def capture_clipboard_once(store: ClipboardStore):
    try:
        # Check for sensitive MIME types first to skip recording
        try:
            types = subprocess.check_output(["wl-paste", "--list-types"], stderr=subprocess.DEVNULL, timeout=0.5).decode("utf-8", errors="ignore")
            if "x-kde-passwordManagerHint" in types:
                return
        except Exception:
            pass

        image_data = _capture_image()
        if image_data:
            store.add_image(image_data)
            return

        result = subprocess.run(_clipboard_cmd(), capture_output=True, timeout=1)
        text = result.stdout.decode("utf-8", errors="replace").strip()
        if text:
            store.add(text)
    except FileNotFoundError:
        pass
    except Exception:
        pass


@dataclass
class CustomPrompt:
    id: str
    name: str
    prompt: str
    categories: Optional[List[str]] = None
    action_type: str = "web"
    bound_model_alias: Optional[str] = None


# LLM inference parameter defaults
DEFAULT_TEMPERATURE = 1.0
DEFAULT_MAX_TOKENS = 4096
DEFAULT_TOP_P = 1.0


@dataclass
class LLMModelConfig:
    alias: str
    base_url: str
    api_key: str
    model_name: str
    is_default: bool = False
    is_title_model: bool = False
    is_subagent_default: bool = False  # 标记为子代理（sub_agent 工具）默认模型
    temperature: float = DEFAULT_TEMPERATURE
    max_tokens: int = DEFAULT_MAX_TOKENS
    top_p: float = DEFAULT_TOP_P
    thinking_enabled: bool = False
    reasoning_effort: str = "high"


LLM_SETTINGS_PATH = os.path.join(CONFIG_DIR, "llm_settings.json")


class LLMSettingsStore:
    def __init__(self):
        self.models: List[LLMModelConfig] = []
        self._load()

    def _load(self):
        if not os.path.isfile(LLM_SETTINGS_PATH):
            self.models = [
                LLMModelConfig(
                    alias="Default",
                    base_url="https://api.deepseek.com/v1",
                    api_key="",
                    model_name="deepseek-chat",
                    is_default=True
                )
            ]
            return
        try:
            with open(LLM_SETTINGS_PATH) as f:
                data = json.load(f)
            
            if isinstance(data, dict) and "models" in data:
                self.models = []
                for m in data["models"]:
                    self.models.append(LLMModelConfig(
                        alias=m.get("alias", "Unnamed"),
                        base_url=m.get("base_url", ""),
                        api_key=m.get("api_key", ""),
                        model_name=m.get("model_name", ""),
                        is_default=m.get("is_default", False),
                        is_title_model=m.get("is_title_model", False),
                        is_subagent_default=m.get("is_subagent_default", False),
                        temperature=m.get("temperature", DEFAULT_TEMPERATURE),
                        max_tokens=m.get("max_tokens", DEFAULT_MAX_TOKENS),
                        top_p=m.get("top_p", DEFAULT_TOP_P),
                        thinking_enabled=m.get("thinking_enabled", False),
                        reasoning_effort=m.get("reasoning_effort", "high"),
                    ))
            else:
                # Migrate old format
                api_key = data.get("api_key", "")
                base_url = data.get("base_url", "https://api.deepseek.com/v1")
                model_name = data.get("model_name", "deepseek-chat")
                self.models = [
                    LLMModelConfig(
                        alias="Default",
                        base_url=base_url,
                        api_key=api_key,
                        model_name=model_name,
                        is_default=True
                    )
                ]
                self.save_all()
        except Exception:
            self.models = [
                LLMModelConfig(
                    alias="Default",
                    base_url="https://api.deepseek.com/v1",
                    api_key="",
                    model_name="deepseek-chat",
                    is_default=True
                )
            ]

    @property
    def api_key(self) -> str:
        default_model = next((m for m in self.models if m.is_default), None)
        if not default_model and self.models:
            default_model = self.models[0]
        return default_model.api_key if default_model else ""

    @property
    def base_url(self) -> str:
        default_model = next((m for m in self.models if m.is_default), None)
        if not default_model and self.models:
            default_model = self.models[0]
        return default_model.base_url if default_model else "https://api.deepseek.com/v1"

    @property
    def model_name(self) -> str:
        default_model = next((m for m in self.models if m.is_default), None)
        if not default_model and self.models:
            default_model = self.models[0]
        return default_model.model_name if default_model else "deepseek-chat"

    def save_all(self):
        os.makedirs(CONFIG_DIR, exist_ok=True)
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
        try:
            fd = os.open(LLM_SETTINGS_PATH, flags, 0o600)
            with os.fdopen(fd, "w") as f:
                json.dump({
                    "version": 2,
                    "models": [asdict(m) for m in self.models]
                }, f, indent=2)
        except Exception as e:
            print(f"Error saving LLM settings: {e}", flush=True)


class CustomPromptsStore:
    def __init__(self):
        self._prompts: List[CustomPrompt] = []
        self._load()

    def _load(self):
        if not os.path.isfile(CUSTOM_PROMPTS_PATH):
            self._prompts = [
                CustomPrompt(
                    id=str(uuid4()),
                    name="Ask Google",
                    prompt="以上内容是什么意思，如果是代码，请分析并注释。",
                    categories=["text"],
                    action_type="web"
                )
            ]
            self._save()
            return
        try:
            with open(CUSTOM_PROMPTS_PATH) as f:
                data = json.load(f)
            self._prompts = []
            for p in data:
                if "categories" not in p or not p["categories"]:
                    p["categories"] = ["text"]
                if "action_type" not in p:
                    p["action_type"] = "web"
                # Filter out unknown keys to prevent TypeError when loading future/past structures
                allowed_keys = {"id", "name", "prompt", "categories", "action_type", "bound_model_alias"}
                filtered_p = {k: v for k, v in p.items() if k in allowed_keys}
                self._prompts.append(CustomPrompt(**filtered_p))
            if not self._prompts:
                self._prompts = [
                    CustomPrompt(
                        id=str(uuid4()),
                        name="Ask Google",
                        prompt="以上内容是什么意思，如果是代码，请分析并注释。",
                        categories=["text"],
                        action_type="web"
                    )
                ]
                self._save()
        except (json.JSONDecodeError, TypeError, KeyError):
            self._prompts = [
                CustomPrompt(
                    id=str(uuid4()),
                    name="Ask Google",
                    prompt="以上内容是什么意思，如果是代码，请分析并注释。",
                    categories=["text"],
                    action_type="web"
                )
            ]
            self._save()

    def _save(self):
        os.makedirs(CONFIG_DIR, exist_ok=True)
        with open(CUSTOM_PROMPTS_PATH, "w") as f:
            json.dump([asdict(p) for p in self._prompts], f, indent=2)

    def get_all(self) -> List[CustomPrompt]:
        return deepcopy(self._prompts)

    def save_all(self, prompts: List[CustomPrompt]):
        self._prompts = deepcopy(prompts)
        self._save()


# ── QQ Mail IMAP Credentials ────────────────────────────────────────────────

QQ_MAIL_CREDENTIALS_PATH = os.path.join(CONFIG_DIR, "qq_mail_credentials.json")


class QQMailCredentialsStore:
    def __init__(self):
        self.email = ""
        self.auth_code = ""
        self.max_body_chars: int = 500
        self._load()

    def _load(self):
        if not os.path.isfile(QQ_MAIL_CREDENTIALS_PATH):
            return
        try:
            with open(QQ_MAIL_CREDENTIALS_PATH) as f:
                data = json.load(f)
            self.email = data.get("email", "")
            self.auth_code = data.get("auth_code", "")
            self.max_body_chars = data.get("max_body_chars", 500)
        except Exception:
            pass

    def save(self):
        os.makedirs(CONFIG_DIR, exist_ok=True)
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
        try:
            fd = os.open(QQ_MAIL_CREDENTIALS_PATH, flags, 0o600)
            with os.fdopen(fd, "w") as f:
                json.dump({
                    "version": 2,
                    "email": self.email,
                    "auth_code": self.auth_code,
                    "max_body_chars": self.max_body_chars,
                }, f, indent=2)
        except Exception as e:
            print(f"Error saving QQ mail credentials: {e}", flush=True)


# ── Gmail OAuth 2.0 Credentials ────────────────────────────────────────────

GMAIL_CREDENTIALS_DIR = os.path.join(CONFIG_DIR, "gmail_credentials")


class GmailOAuthStore:
    """管理 Gmail OAuth 2.0 凭据的存储路径和授权状态。

    职责：
    - 管理 credentials.json（用户从 Google Cloud 下载的 OAuth 客户端 ID）
    - 管理 token.json（OAuth 授权后自动生成/刷新）
    - 提供授权状态检查供设置 UI 使用
    """

    def __init__(self):
        self.email: str = ""
        self.is_authorized: bool = False
        self._load()

    @property
    def credentials_json_path(self) -> str:
        return os.path.join(GMAIL_CREDENTIALS_DIR, "credentials.json")

    @property
    def token_json_path(self) -> str:
        return os.path.join(GMAIL_CREDENTIALS_DIR, "token.json")

    def _load(self):
        """读取 token.json 获取授权状态和邮箱地址。"""
        if not os.path.isfile(self.token_json_path):
            return
        try:
            with open(self.token_json_path) as f:
                data = json.load(f)
            self.is_authorized = True
            self.email = data.get("email", "")
        except Exception:
            pass

    def save_token(self, token_dict: dict, email: str = ""):
        """保存 OAuth token 到磁盘（权限 0o600）。"""
        os.makedirs(GMAIL_CREDENTIALS_DIR, exist_ok=True)
        if email:
            token_dict["email"] = email
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
        try:
            fd = os.open(self.token_json_path, flags, 0o600)
            with os.fdopen(fd, "w") as f:
                json.dump(token_dict, f, indent=2)
        except Exception as e:
            print(f"Error saving Gmail token: {e}", flush=True)
        self.is_authorized = True
        if email:
            self.email = email

    def revoke(self):
        """撤销授权，删除 token 文件。"""
        try:
            if os.path.exists(self.token_json_path):
                os.remove(self.token_json_path)
        except Exception as e:
            print(f"Error revoking Gmail token: {e}", flush=True)
        self.is_authorized = False
        self.email = ""


# ── AI Conversation Truncation Settings ─────────────────────────────────────

AI_SETTINGS_PATH = os.path.join(CONFIG_DIR, "ai_settings.json")

# 摘要压缩默认模板——3 处共享同一常量，修改只需改此处
_DEFAULT_SUMMARY_TEMPLATE = (
    "{prev_summary}请将以下对话压缩为简洁摘要，保留用户需求、决策、偏好、"
    "关键约定（如代码风格、命名规范）和已取得的进展：\n\n"
    "{conversation_text}\n\n"
    "要求：第三人称、客观简洁、不超过{max_chars}字。"
)


class AISettingsStore:
    """应用设置存储（AI 对话设置 + 常量配置），遵循 QQMailCredentialsStore 模式。"""

    def __init__(self):
        self.soft_limit: int = 200      # 触发截断的消息数
        self.trim_target: int = 100     # 裁剪后保留的消息数
        self.enable_summary: bool = True      # 是否启用摘要压缩
        self.summary_threshold: int = 80      # 剩余多少条消息时触发摘要
        self.summary_max_chars: int = 500     # 摘要最大字符数
        self.summary_prompt_template: str = _DEFAULT_SUMMARY_TEMPLATE
        self.system_prompt: str = ""  # 全局 AI 对话系统提示词（新对话建立时快照注入）
        self.max_clipboard: int = 150   # 剪切板最大历史项目数
        self.max_tool_iterations: int = 25  # AI 工具调用最大次数
        self.enable_incremental_tools: bool = True  # v3 增量工具卡片
        self.show_tool_details: bool = True  # 是否渲染工具调用结果详情
        self.enable_code_highlight: bool = True  # 是否启用代码语法高亮
        self.enable_global_skills: bool = True  # 是否启用全局 Skill 自动发现
        self.disabled_skills: list[str] = []  # 禁用的 Skill 名称列表
        # ── 内置工具开关 ──
        self.disabled_tools: list[str] = []  # 禁用的内置工具名称列表
        # ── MCP Server 配置列表 ──
        self.mcp_servers: list[dict] = []  # MCPServerConfig.to_dict() 的列表
        self.enable_answer_notification: bool = True  # 主对话 AI 正式回答结束后弹桌面通知
        self._load()

    def _load(self):
        try:
            with open(AI_SETTINGS_PATH) as f:
                data = json.load(f)
            _ = data.get("version", 1)
            self.soft_limit = data.get("soft_limit", 200)
            self.trim_target = data.get("trim_target", 100)
            self.enable_summary = data.get("enable_summary", True)
            self.summary_threshold = data.get("summary_threshold", 80)
            self.summary_max_chars = data.get("summary_max_chars", 500)
            self.summary_prompt_template = data.get(
                "summary_prompt_template", _DEFAULT_SUMMARY_TEMPLATE
            )
            self.system_prompt = data.get("system_prompt", "")
            self.max_clipboard = data.get("max_clipboard", 150)
            self.max_tool_iterations = data.get("max_tool_iterations", 25)
            self.enable_incremental_tools = data.get("enable_incremental_tools", True)
            self.show_tool_details = data.get("show_tool_details", True)
            self.enable_code_highlight = data.get("enable_code_highlight", True)
            self.enable_global_skills = data.get("enable_global_skills", True)
            self.disabled_skills = data.get("disabled_skills", [])
            self.disabled_tools = data.get("disabled_tools", [])
            self.mcp_servers = data.get("mcp_servers", [])
            self.enable_answer_notification = data.get("enable_answer_notification", True)
        except Exception:
            pass  # 使用默认值

    def save(self):
        os.makedirs(CONFIG_DIR, exist_ok=True)
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
        try:
            fd = os.open(AI_SETTINGS_PATH, flags, 0o600)
            with os.fdopen(fd, "w") as f:
                json.dump({
                    "version": 6,
                    "soft_limit": self.soft_limit,
                    "trim_target": self.trim_target,
                    "enable_summary": self.enable_summary,
                    "summary_threshold": self.summary_threshold,
                    "summary_max_chars": self.summary_max_chars,
                    "summary_prompt_template": self.summary_prompt_template,
                    "system_prompt": self.system_prompt,
                    "max_clipboard": self.max_clipboard,
                    "max_tool_iterations": self.max_tool_iterations,
                    "enable_incremental_tools": self.enable_incremental_tools,
                    "show_tool_details": self.show_tool_details,
                    "enable_code_highlight": self.enable_code_highlight,
                    "enable_global_skills": self.enable_global_skills,
                    "disabled_skills": self.disabled_skills,
                    "disabled_tools": self.disabled_tools,
                    "mcp_servers": self.mcp_servers,
                    "enable_answer_notification": self.enable_answer_notification,
                }, f, indent=2)
        except Exception as e:
            print(f"Error saving AI settings: {e}", flush=True)


# ── Semantic Memory Store ─────────────────────────────────────────────────────

MEMORY_PATH = os.path.join(CONFIG_DIR, "agent_memory.json")


@dataclass
class MemoryItem:
    """单条语义记忆，由 memory_save/memory_recall 工具读写。"""
    key: str
    value: str
    category: str = "general"
    created_at: int = 0
    updated_at: int = 0


# 中英文同义映射，用于 BM25 查询扩展
_SYNONYM_MAP = {
    "名字": "name", "姓名": "name", "我叫": "name", "称呼": "name",
    "偏好": "preference", "喜好": "preference", "喜欢": "preference",
    "部署": "deploy", "发布": "deploy",
    "代码风格": "coding_style", "风格": "coding", "编码": "coding",
    "语言": "language", "编程语言": "language",
    "工作流": "workflow", "流程": "workflow",
    "助理": "assistant", "助手": "assistant", "机器人": "assistant",
    "pdf": "pdf", "txt": "txt",
    "转换": "convert", "转成": "convert",
}


class MemStore:
    """跨 session 语义记忆存储，使用 BM25 全文检索（可选依赖，无 jieba 时降级为基本文本匹配）。"""

    def __init__(self):
        self._items: Dict[str, MemoryItem] = {}
        self._bm25 = None
        self._has_jieba: Optional[bool] = None
        self._load()

    def _tokenize(self, text: str) -> list:
        if self._has_jieba is None:
            try:
                import jieba
                self._has_jieba = True
            except ImportError:
                self._has_jieba = False
        if self._has_jieba:
            import jieba
            text_lower = text.lower().replace("_", " ").replace(":", " ")
            words = list(jieba.cut(text_lower))
            for w in text_lower.split():
                if w not in words:
                    words.append(w)
            return [w for w in words if len(w.strip()) > 0]
        return text.lower().replace("_", " ").replace(":", " ").split()

    def _expand_query(self, query: str) -> str:
        parts = [query]
        for zh, en in _SYNONYM_MAP.items():
            if zh in query:
                parts.append(en)
            if en in query.lower():
                parts.append(zh)
        return " ".join(parts)

    def _build_index(self):
        if not self._items:
            self._bm25 = None
            return
        try:
            from rank_bm25 import BM25Okapi
            corpus = [f"{item.key}: {item.value}" for item in self._items.values()]
            tokenized = [self._tokenize(doc) for doc in corpus]
            self._bm25 = BM25Okapi(tokenized)
        except ImportError:
            self._bm25 = None

    def _load(self):
        try:
            with open(MEMORY_PATH) as f:
                data = json.load(f)
            for item_data in data.get("items", []):
                item = MemoryItem(**item_data)
                self._items[item.key] = item
        except Exception:
            pass
        self._build_index()

    def save(self):
        try:
            write_json_private(MEMORY_PATH, {
                "items": [asdict(item) for item in self._items.values()]
            }, indent=2)
        except Exception as e:
            print(f"Error saving memory store: {e}", flush=True)

    def put(self, key: str, value: str):
        now = int(time.time() * 1000)
        if key in self._items:
            item = self._items[key]
            item.value = value
            item.updated_at = now
        else:
            self._items[key] = MemoryItem(
                key=key, value=value, created_at=now, updated_at=now,
            )
        self._build_index()

    def get(self, key: str) -> Optional[MemoryItem]:
        return self._items.get(key)

    def search(self, query: str, limit: int = 10) -> List[MemoryItem]:
        if not self._items:
            return []
        if self._bm25:
            expanded = self._expand_query(query)
            tokens = self._tokenize(expanded)
            scores = self._bm25.get_scores(tokens)
            ranked = sorted(
                zip(list(self._items.values()), scores),
                key=lambda x: x[1], reverse=True,
            )
            return [item for item, score in ranked[:limit] if score > 0]
        # 降级：无 BM25 时基本子串匹配（含同义映射）
        keywords = {query.lower()}
        expanded = self._expand_query(query)
        for kw in expanded.split():
            keywords.add(kw.lower())
        results = []
        for item in self._items.values():
            text = (item.key + " " + item.value).lower()
            if any(kw in text for kw in keywords):
                results.append(item)
        return results[:limit]

    def delete(self, key: str):
        self._items.pop(key, None)
        self._build_index()

    def list_recent(self, limit: int = 20) -> List[MemoryItem]:
        sorted_items = sorted(
            self._items.values(), key=lambda x: x.updated_at, reverse=True
        )
        return sorted_items[:limit]
