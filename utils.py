import time
import os


def is_wayland() -> bool:
    """Return True if the current session is Wayland."""
    return (
        os.environ.get("XDG_SESSION_TYPE", "").lower() == "wayland"
        or "WAYLAND_DISPLAY" in os.environ
    )


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


CACHE_DIR = os.path.expanduser("~/.cache/opencode-switcher")
CONVERSATIONS_DIR = os.path.join(CACHE_DIR, "conversations")

def request_window_focus(wm_class: str):
    """向 GNOME 扩展发送窗口聚焦请求"""
    try:
        cache_dir = os.path.expanduser("~/.cache/opencode-switcher")
        os.makedirs(cache_dir, exist_ok=True)
        with open(os.path.join(cache_dir, "focus.request"), "w") as f:
            f.write(wm_class)
    except Exception as e:
        print(f"Failed to write focus request: {e}", flush=True)

