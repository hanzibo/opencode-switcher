import json
import re
import os

CODE_KEYWORDS_RE = re.compile(r'\b(const|function|export)\b')
CURLY_NEWLINE_RE = re.compile(r'[\{\}]\s*[\r\n]|[\r\n]\s*[\{\}]')
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
PROMPTS_PATH = os.path.join(CONFIG_DIR, "prompts.json")
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


@dataclass
class Prompt:
    title: str
    text: str
    timestamp: int


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


class ClipboardStore:
    def __init__(self):
        self._items: List[ClipboardItem] = []
        self._last_written_hash: Optional[str] = None
        self._load()

    def _load(self):
        if not os.path.isfile(CLIPBOARD_PATH):
            return
        try:
            with open(CLIPBOARD_PATH, "r") as f:
                data = json.load(f)
            self._items = [ClipboardItem(**d) for d in data[-MAX_CLIPBOARD:]]
        except (json.JSONDecodeError, TypeError):
            self._items = []

    def _save(self):
        os.makedirs(CONFIG_DIR, exist_ok=True)
        with open(CLIPBOARD_PATH, "w") as f:
            json.dump([asdict(i) for i in self._items], f)

    def classify_text(self, text: str) -> str:
        stripped = text.strip()
        if stripped.startswith("http"):
            return "link"
        
        # Check JS/Gjs keywords: const, function, export
        # Check curly braces with newline: {\s*\n or \n\s*}
        has_keywords = bool(CODE_KEYWORDS_RE.search(text))
        has_curly_newline = bool(CURLY_NEWLINE_RE.search(text))
        if has_keywords or has_curly_newline:
            return "code"
            
        return "text"

    def add(self, text: str):
        if not text.strip():
            return
        h = _content_hash(text)
        if h == self._last_written_hash:
            return
        if self._items and self._items[-1].hash == h:
            return
        item_type = self.classify_text(text)
        self._items.append(ClipboardItem(text=text, timestamp=int(time.time() * 1000), hash=h, type=item_type))
        if len(self._items) > MAX_CLIPBOARD:
            self._items = self._items[-MAX_CLIPBOARD:]
        self._save()

    def add_image(self, image_data: bytes):
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
        if 0 <= index < len(self._items):
            self._items.pop(index)
            self._save()

    def clear_all(self):
        self._items.clear()
        self._save()

    def get_all(self) -> List[ClipboardItem]:
        return list(self._items)

    def reload(self):
        self._load()

    def mark_written(self, text: str, item_hash: Optional[str] = None):
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


class PromptStore:
    def __init__(self):
        self._prompts: List[Prompt] = []
        self._load()

    def _load(self):
        if not os.path.isfile(PROMPTS_PATH):
            return
        try:
            with open(PROMPTS_PATH) as f:
                data = json.load(f)
            self._prompts = [Prompt(**d) for d in data]
        except (json.JSONDecodeError, TypeError):
            self._prompts = []

    def _save(self):
        os.makedirs(CONFIG_DIR, exist_ok=True)
        with open(PROMPTS_PATH, "w") as f:
            json.dump([asdict(p) for p in self._prompts], f)

    def create(self, title: str, text: str):
        self._prompts.append(Prompt(title=title, text=text, timestamp=int(time.time() * 1000)))
        self._save()

    def update(self, index: int, title: str, text: str):
        if 0 <= index < len(self._prompts):
            self._prompts[index] = Prompt(title=title, text=text, timestamp=int(time.time() * 1000))
            self._save()

    def delete(self, index: int):
        if 0 <= index < len(self._prompts):
            self._prompts.pop(index)
            self._save()

    def get_all(self) -> List[Prompt]:
        return list(self._prompts)

    def reload(self):
        self._load()


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

    def migrate_from_prompts(self):
        if not os.path.isfile(PROMPTS_PATH):
            return
        try:
            with open(PROMPTS_PATH) as f:
                data = json.load(f)
            prompts = [Prompt(**d) for d in data]
        except (json.JSONDecodeError, TypeError):
            return
        if not prompts:
            return
        items = [CategoryItem(title=p.title, text=p.text, timestamp=p.timestamp) for p in prompts]
        self._categories.append(CustomCategory(
            id=uuid4().hex[:12], name="Prompts", items=items,
            pinned=False, created_at=int(time.time() * 1000)
        ))


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
                    prompt="以上内容是什么意思，如果是代码，请分析并注释。"
                )
            ]
            self._save()
            return
        try:
            with open(CUSTOM_PROMPTS_PATH) as f:
                data = json.load(f)
            self._prompts = [CustomPrompt(**p) for p in data]
            if not self._prompts:
                self._prompts = [
                    CustomPrompt(
                        id=str(uuid4()),
                        name="Ask Google",
                        prompt="以上内容是什么意思，如果是代码，请分析并注释。"
                    )
                ]
                self._save()
        except (json.JSONDecodeError, TypeError, KeyError):
            self._prompts = [
                CustomPrompt(
                    id=str(uuid4()),
                    name="Ask Google",
                    prompt="以上内容是什么意思，如果是代码，请分析并注释。"
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

