import subprocess
import shutil
import shlex
import threading
import time
from typing import Optional, Set


def _find_gnome_terminal() -> Optional[str]:
    return shutil.which("gnome-terminal")


def _find_opencode() -> Optional[str]:
    return shutil.which("opencode")


def _get_terminal_windows() -> Set[str]:
    try:
        out = subprocess.check_output(
            ["xdotool", "search", "--onlyvisible", "--class", "gnome-terminal"],
            stderr=subprocess.DEVNULL, timeout=3,
        ).decode().strip()
        return set(out.splitlines()) if out else set()
    except Exception:
        return set()


def _activate_new_window(before: Set[str]):
    for _ in range(20):
        time.sleep(0.15)
        after = _get_terminal_windows()
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


def launch_session(session_id: str, directory: str) -> Optional[str]:
    terminal = _find_gnome_terminal()
    if not terminal:
        return "gnome-terminal not found"
    opencode = _find_opencode()
    if not opencode:
        return "opencode not found in PATH"
    try:
        before = _get_terminal_windows()
        quoted_dir = shlex.quote(directory)
        quoted_id = shlex.quote(session_id)
        shell_cmd = f"cd {quoted_dir} && exec {shlex.quote(opencode)} --session {quoted_id}"
        subprocess.Popen(
            [terminal, "--", "bash", "-c", shell_cmd],
            start_new_session=True,
        )
        threading.Thread(
            target=_activate_new_window, args=(before,), daemon=True
        ).start()
        return None
    except FileNotFoundError:
        return "gnome-terminal not found"
    except Exception as e:
        return str(e)


def launch_new_session(directory: str) -> Optional[str]:
    terminal = _find_gnome_terminal()
    if not terminal:
        return "gnome-terminal not found"
    opencode = _find_opencode()
    if not opencode:
        return "opencode not found in PATH"
    try:
        before = _get_terminal_windows()
        quoted_dir = shlex.quote(directory)
        shell_cmd = f"cd {quoted_dir} && exec {shlex.quote(opencode)}"
        subprocess.Popen(
            [terminal, "--", "bash", "-c", shell_cmd],
            start_new_session=True,
        )
        threading.Thread(
            target=_activate_new_window, args=(before,), daemon=True
        ).start()
        return None
    except FileNotFoundError:
        return "gnome-terminal not found"
    except Exception as e:
        return str(e)
