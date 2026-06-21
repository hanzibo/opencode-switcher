import json
import re
import os
import threading

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
from typing import Optional, List, Dict, Any
from uuid import uuid4

from utils import is_wayland

CONFIG_DIR = os.path.expanduser("~/.config/opencode-switcher")
CLIPBOARD_PATH = os.path.join(CONFIG_DIR, "clipboard_history.json")
# ponytail: removed unused Prompt, PromptStore, and migrate_from_prompts
MAX_CLIPBOARD = 150
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
        with self._lock:
            self._load()

    def _load(self):
        with self._lock:
            if not os.path.isfile(CLIPBOARD_PATH):
                return
            try:
                with open(CLIPBOARD_PATH, "r") as f:
                    data = json.load(f)
                self._items = [ClipboardItem(**d) for d in data[-MAX_CLIPBOARD:]]
            except (json.JSONDecodeError, TypeError):
                self._items = []

    def _save(self):
        with self._lock:
            os.makedirs(CONFIG_DIR, exist_ok=True)
            with open(CLIPBOARD_PATH, "w") as f:
                json.dump([asdict(i) for i in self._items], f)

    def classify_text(self, text: str) -> str:
        return classify_text(text)

    def detect_language_name(self, text: str) -> Optional[str]:
        return detect_language_name(text)

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
            if len(self._items) > MAX_CLIPBOARD:
                self._items = self._items[-MAX_CLIPBOARD:]
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
            if len(self._items) > MAX_CLIPBOARD:
                self._items = self._items[-MAX_CLIPBOARD:]
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
                self._items.pop(index)
                self._save()

    def clear_all(self):
        with self._lock:
            self._items.clear()
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


def _capture_image() -> Optional[bytes]:
    if is_wayland():
        try:
            types = subprocess.check_output(["wl-paste", "--list-types"], stderr=subprocess.DEVNULL, timeout=0.5).decode("utf-8", errors="ignore")
            if "image/png" in types:
                res = subprocess.run(["wl-paste", "--type", "image/png"], capture_output=True, timeout=1)
                if res.returncode == 0 and res.stdout:
                    return res.stdout
        except Exception:
            pass
    else:
        try:
            targets = subprocess.check_output(["xclip", "-selection", "clipboard", "-t", "TARGETS", "-o"], stderr=subprocess.DEVNULL, timeout=0.5).decode("utf-8", errors="ignore")
            if "image/png" in targets:
                res = subprocess.run(["xclip", "-selection", "clipboard", "-t", "image/png", "-o"], capture_output=True, timeout=1)
                if res.returncode == 0 and res.stdout:
                    return res.stdout
        except Exception:
            pass
    return None


def _clipboard_cmd() -> list:
    if is_wayland():
        return ["wl-paste"]
    return ["xclip", "-o", "-selection", "clipboard"]


def capture_clipboard_once(store: ClipboardStore):
    try:
        # Check for sensitive MIME types first to skip recording
        if is_wayland():
            try:
                types = subprocess.check_output(["wl-paste", "--list-types"], stderr=subprocess.DEVNULL, timeout=0.5).decode("utf-8", errors="ignore")
                if "x-kde-passwordManagerHint" in types:
                    return
            except Exception:
                pass
        else:
            try:
                targets = subprocess.check_output(["xclip", "-selection", "clipboard", "-t", "TARGETS", "-o"], stderr=subprocess.DEVNULL, timeout=0.5).decode("utf-8", errors="ignore")
                if "x-kde-passwordManagerHint" in targets:
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


LLM_SETTINGS_PATH = os.path.join(CONFIG_DIR, "llm_settings.json")


class LLMSettingsStore:
    def __init__(self):
        self.api_key = ""
        self.base_url = "https://api.deepseek.com/v1"
        self.model_name = "deepseek-chat"
        self._load()

    def _load(self):
        if not os.path.isfile(LLM_SETTINGS_PATH):
            return
        try:
            with open(LLM_SETTINGS_PATH) as f:
                data = json.load(f)
            self.api_key = data.get("api_key", "")
            self.base_url = data.get("base_url", "https://api.deepseek.com/v1")
            self.model_name = data.get("model_name", "deepseek-chat")
        except Exception:
            pass

    def save(self, api_key: str, base_url: str, model_name: str):
        self.api_key = api_key
        self.base_url = base_url
        self.model_name = model_name
        os.makedirs(CONFIG_DIR, exist_ok=True)
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
        try:
            fd = os.open(LLM_SETTINGS_PATH, flags, 0o600)
            with os.fdopen(fd, "w") as f:
                json.dump({
                    "api_key": self.api_key,
                    "base_url": self.base_url,
                    "model_name": self.model_name
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
                allowed_keys = {"id", "name", "prompt", "categories", "action_type"}
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

