import os
import subprocess
import shutil
import shlex
from typing import Optional

_TERMINALS = ["ptyxis", "gnome-terminal", "kgx", "blackbox"]


def _find_terminal() -> Optional[str]:
    for term in _TERMINALS:
        path = shutil.which(term)
        if path:
            return path
    return None


def _find_opencode() -> Optional[str]:
    return shutil.which("opencode")


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

        subprocess.Popen(
            [terminal, "--", "bash", "-c", shell_cmd],
            start_new_session=True,
        )
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
