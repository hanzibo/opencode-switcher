import json
import time
import os


def relative_time(ts_ms: int) -> str:
    """Convert a millisecond timestamp to a human-readable relative time string."""
    if not ts_ms:
        return ""
    delta = time.time() * 1000 - ts_ms
    if delta < 0:
        return "now"
    secs = delta / 1000
    if secs < 60:
        return "now"
    mins = secs / 60
    if mins < 60:
        return f"{int(mins)}m ago"
    hours = mins / 60
    if hours < 24:
        return f"{int(hours)}h ago"
    days = hours / 24
    if days < 7:
        return f"{int(days)}d ago"
    weeks = days / 7
    return f"{int(weeks)}w ago"


def write_json_private(path: str, data: dict, indent: int = 2) -> None:
    """Write JSON data to ``path`` with user-private permissions (0o600).

    Mirrors the 0600 write pattern used by the credential stores
    (``stores/clipboard_store.py``, ``tool_registry/gmail.py``): the file is
    created with mode 0o600, and any pre-existing file is chmodded to 0o600
    afterwards so umask-created 0644 files get tightened on the next save.
    """
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=indent, ensure_ascii=False)
    # os.open's mode only applies at creation; tighten pre-existing files too.
    os.chmod(path, 0o600)


CACHE_DIR = os.path.expanduser("~/.cache/opencode-switcher")
CONVERSATIONS_DIR = os.path.join(CACHE_DIR, "conversations")
PANEL_WIDTH = 1320


# ── Permission hardening helpers ──────────────────────────────────────────────
#
# These tighten legacy files created before the 0600 write pattern (e.g. by
# the old plain ``open(path, "w")`` calls) and protect private data dirs.
# They are deliberately narrow: only known user-sensitive paths are touched,
# symlinks are never followed, and missing/inaccessible paths are a safe no-op.


def harden_file_private(path: str, mode: int = 0o600) -> bool:
    """Tighten a sensitive file's mode to ``mode`` (0600 by default).

    Never follows symlinks: a symlinked path is either lchmod'd (mode applied
    to the link itself, not its target) or skipped when the platform does not
    support ``os.lchmod``. Missing files are a safe no-op. Returns True when
    a mode was applied.
    """
    try:
        if not os.path.lexists(path):
            return False
        if os.path.islink(path):
            try:
                os.lchmod(path, mode)
            except (AttributeError, NotImplementedError, OSError):
                return False
            return True
        os.chmod(path, mode)
        return True
    except OSError:
        return False


def harden_dir_private(path: str, mode: int = 0o700) -> bool:
    """Ensure a sensitive directory exists with user-private mode ``mode``.

    Creates missing directories (mode applies at creation) and tightens
    pre-existing ones. Symlinked directories are skipped so the chmod never
    lands on an unrelated target. Missing/inaccessible paths never raise.
    Returns True when a mode was applied.
    """
    try:
        if os.path.islink(path):
            return False
        os.makedirs(path, mode=mode, exist_ok=True)
        os.chmod(path, mode)
        return True
    except OSError:
        return False


def harden_json_files_in_dir(dir_path: str, mode: int = 0o600) -> int:
    """Tighten every ``*.json`` inside a private data dir (e.g. conversations).

    Only direct children are touched — the directory is never walked
    recursively, so unrelated nested files are left alone. Missing or
    unreadable directories return 0 without raising. Returns the number of
    files whose mode was applied.
    """
    try:
        names = os.listdir(dir_path)
    except OSError:
        return 0
    count = 0
    for name in names:
        if not name.endswith(".json"):
            continue
        if harden_file_private(os.path.join(dir_path, name), mode):
            count += 1
    return count


def sweep_sensitive_permissions() -> None:
    """Startup sweep: tighten permissions on known sensitive user data.

    Files -> 0o600 (clipboard history + backup, conversation JSONs + index,
    agent memory, todos, credential/config JSONs); private data dirs -> 0o700
    (config dir, conversations dir, gmail credentials dir).

    Paths are resolved lazily from their owning modules so the sweep picks up
    patched values in tests and never triggers a circular import. Safe for
    missing files/dirs, symlinks, and permission errors; only the explicitly
    listed paths are touched — never unrelated project files.
    """
    # Lazy imports: stores/ imports system.utils at module load, so pulling
    # their path constants here (at call time) avoids any import cycle.
    from stores.clipboard_store import (
        AI_SETTINGS_PATH,
        CATEGORIES_PATH,
        CLIPBOARD_PATH,
        CONFIG_DIR,
        ConversationStore,
        CUSTOM_PROMPTS_PATH,
        GMAIL_CREDENTIALS_DIR,
        LLM_SETTINGS_PATH,
        MEMORY_PATH,
        QQ_MAIL_CREDENTIALS_PATH,
    )
    from tool_registry.todo import _TODO_PATH

    file_paths = (
        CLIPBOARD_PATH,
        CLIPBOARD_PATH + ".backup",
        MEMORY_PATH,
        _TODO_PATH,
        LLM_SETTINGS_PATH,
        QQ_MAIL_CREDENTIALS_PATH,
        AI_SETTINGS_PATH,
        CATEGORIES_PATH,
        CUSTOM_PROMPTS_PATH,
        os.path.join(GMAIL_CREDENTIALS_DIR, "credentials.json"),
        os.path.join(GMAIL_CREDENTIALS_DIR, "token.json"),
        os.path.join(CONVERSATIONS_DIR, ConversationStore._INDEX_FILENAME),
    )
    dir_paths = (CONFIG_DIR, CONVERSATIONS_DIR, GMAIL_CREDENTIALS_DIR)

    for path in file_paths:
        harden_file_private(path, 0o600)
    for path in dir_paths:
        harden_dir_private(path, 0o700)
    harden_json_files_in_dir(CONVERSATIONS_DIR, 0o600)

def request_window_focus(wm_class: str):
    """向 GNOME 扩展发送窗口聚焦请求"""
    try:
        cache_dir = os.path.expanduser("~/.cache/opencode-switcher")
        os.makedirs(cache_dir, exist_ok=True)
        with open(os.path.join(cache_dir, "focus.request"), "w") as f:
            f.write(wm_class)
    except Exception as e:
        print(f"Failed to write focus request: {e}", flush=True)

