import os
import subprocess
import shutil
import shlex
import threading
import time
from typing import Optional, Set
from utils import is_wayland

_TERMINALS = ["ptyxis", "gnome-terminal", "kgx", "blackbox"]

# ponytail: removed redundant _on_wayland wrapper

def _find_terminal() -> Optional[str]:
    for term in _TERMINALS:
        path = shutil.which(term)
        if path:
            return path
    return None


def _find_opencode() -> Optional[str]:
    return shutil.which("opencode")


def _map_terminal_to_class(terminal_path: str) -> str:
    basename = os.path.basename(terminal_path).lower()
    if "gnome-terminal" in basename:
        return "gnome-terminal"
    elif "ptyxis" in basename:
        return "ptyxis|org.gnome.Ptyxis"
    elif "kgx" in basename or "console" in basename:
        return "kgx|org.gnome.Console"
    elif "blackbox" in basename:
        return "blackbox|com.raggesilver.BlackBox"
    return basename


def _get_terminal_windows(terminal: str) -> Set[str]:
    if not terminal:
        return set()
    class_pattern = _map_terminal_to_class(terminal)

    try:
        out = subprocess.check_output(
            ["xdotool", "search", "--onlyvisible", "--class", class_pattern],
            stderr=subprocess.DEVNULL, timeout=3,
        ).decode().strip()
        return set(out.splitlines()) if out else set()
    except Exception:
        return set()


def _activate_new_window(before: Set[str], terminal: str):
    for _ in range(20):
        time.sleep(0.15)
        after = _get_terminal_windows(terminal)
        new_ids = after - before
        if new_ids:
            wid = next(iter(new_ids))
            try:
                subprocess.run(
                    ["xdotool", "windowactivate", wid],
                    stderr=subprocess.DEVNULL, timeout=2,
                )
            except Exception:
                pass
            return


def _launch(terminal: str, opencode: str, directory: str,
            session_id: Optional[str] = None,
            pure: bool = False) -> Optional[str]:
    try:
        quoted_dir = shlex.quote(directory)
        quoted_opencode = shlex.quote(opencode)
        pure_flag = " --pure" if pure else ""
        if session_id:
            quoted_id = shlex.quote(session_id)
            shell_cmd = f"cd {quoted_dir} && exec {quoted_opencode}{pure_flag} --session {quoted_id}"
        else:
            shell_cmd = f"cd {quoted_dir} && exec {quoted_opencode}{pure_flag}"

        before = _get_terminal_windows(terminal) if not is_wayland() else set()
        subprocess.Popen(
            [terminal, "--", "bash", "-c", shell_cmd],
            start_new_session=True,
        )
        if not is_wayland():
            threading.Thread(
                target=_activate_new_window, args=(before, terminal), daemon=True
            ).start()
        return None
    except FileNotFoundError:
        return f"{terminal} not found"
    except Exception as e:
        return str(e)

def _resolve_deps() -> tuple:
    """Find terminal and opencode, return (terminal, opencode) or (None, error_msg)."""
    terminal = _find_terminal()
    if not terminal:
        return None, "No supported terminal found (ptyxis/gnome-terminal/kgx)"
    opencode = _find_opencode()
    if not opencode:
        return None, "opencode not found in PATH"
    return terminal, opencode


def launch_session(session_id: str, directory: str) -> Optional[str]:
    terminal, opencode_or_err = _resolve_deps()
    if not terminal:
        return opencode_or_err
    return _launch(terminal, opencode_or_err, directory, session_id)


def launch_new_session(directory: str) -> Optional[str]:
    terminal, opencode_or_err = _resolve_deps()
    if not terminal:
        return opencode_or_err
    return _launch(terminal, opencode_or_err, directory)


def launch_session_pure(session_id: str, directory: str) -> Optional[str]:
    terminal, opencode_or_err = _resolve_deps()
    if not terminal:
        return opencode_or_err
    return _launch(terminal, opencode_or_err, directory, session_id, pure=True)
