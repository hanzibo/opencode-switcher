"""Bash execution tool — persistent bash session with command execution."""

import os
import select
import subprocess
import tempfile
import time
from typing import Final, Optional

from ._state import bash as _bash_state


_MAX_BASH_OUTPUT_CHARS = 5000
_BASH_TIMEOUT_DEFAULT = 60
_BASH_SHELL = "/bin/bash"

_ALWAYS_INTERACTIVE: Final[frozenset] = frozenset({
    "vi", "vim", "nvim", "nano", "emacs", "vimdiff",
    "less", "more", "most",
    "top", "htop", "btop", "iftop", "iotop",
})

_DUAL_MODE: Final[frozenset] = frozenset({
    "python", "python3", "ipython",
    "node", "irb",
    "bash", "zsh", "sh", "dash", "fish",
})


def _check_interactive(command: str) -> Optional[str]:
    if not command or not command.strip():
        return None
    parts = command.strip().split(maxsplit=1)
    first_word = parts[0].strip()
    has_args = len(parts) > 1 and parts[1].strip()

    if first_word in _ALWAYS_INTERACTIVE:
        return (f"错误：不支持交互式命令「{first_word}」。\n"
                f"   该命令需要 TTY 终端，无法在后台管道模式下执行。")

    if first_word in _DUAL_MODE and not has_args:
        return (f"错误：裸启动「{first_word}」会进入交互模式。\n"
                f"   如需执行脚本，请提供参数，例如: {first_word} script.py")

    return None


def _save_truncated_output(output: str, command: str) -> str:
    tmp_dir = tempfile.gettempdir()
    _clean_old_temp_files("bash_out_", tmp_dir)
    cmd_hash = hash(command) & 0xFFFFFFFF
    prefix = f"bash_out_{cmd_hash:08x}_"
    try:
        with tempfile.NamedTemporaryFile(
            mode='w', dir=tmp_dir, prefix=prefix, suffix='.txt',
            delete=False, encoding='utf-8',
        ) as f:
            f.write(output)
            return f"\n完整输出已保存至: {f.name}"
    except OSError:
        return ""


def _clean_old_temp_files(prefix: str, tmp_dir: str, max_age: int = 86400):
    now = time.time()
    for entry in os.listdir(tmp_dir):
        if entry.startswith(prefix):
            path = os.path.join(tmp_dir, entry)
            try:
                if now - os.path.getmtime(path) > max_age:
                    os.remove(path)
            except OSError:
                pass


class _BashSession:
    """Persistent bash session that maintains state across command executions.

    Uses binary pipe I/O (bypassing Python's TextIOWrapper buffering) with a
    sentinel protocol to reliably detect command completion and capture exit
    codes. On timeout the session enters an error state and must be restarted.
    """

    _SENTINEL_B = b",,,,bash-exit-"
    _SENTINEL_END_B = b"-banner,,,,"
    _process: Optional["subprocess.Popen[bytes]"] = None

    def __init__(self):
        self.process: Optional["subprocess.Popen[bytes]"] = None
        self._timed_out = False
        self._started = False

    def start(self):
        """Spawn a new persistent bash subprocess (binary pipe mode)."""
        self.process = subprocess.Popen(
            [_BASH_SHELL],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
            cwd=_bash_state.cwd,
            preexec_fn=os.setsid if hasattr(os, "setsid") else None,
        )
        self._started = True

    def execute(self, command: str, timeout: int = _BASH_TIMEOUT_DEFAULT,
                cancel_event=None) -> dict:
        if self._timed_out:
            raise RuntimeError("Bash session has timed out and must be restarted (restart=True).")
        if not self._started or self.process is None:
            self.start()
        if self.process is None:
            return {"output": "错误：Bash 进程未能启动", "exit_code": -1, "timed_out": False}

        process = self.process
        if process.returncode is not None:
            return {"output": "错误：Bash 进程已意外退出", "exit_code": process.returncode, "timed_out": False}

        sentinel_cmd_b = (
            b"{ " + command.encode("utf-8", errors="replace")
            + b"; } 2>&1; echo "
            + self._SENTINEL_B + b"$?" + self._SENTINEL_END_B + b"\n"
        )

        try:
            process.stdin.write(sentinel_cmd_b)
            process.stdin.flush()
        except BrokenPipeError:
            return {"output": "错误：Bash 进程已关闭（stdin 写入失败）", "exit_code": -1, "timed_out": False}

        output_buf = bytearray()
        sentinel_found = False
        exit_code = -1
        fd = process.stdout.fileno()

        poll = select.poll()
        poll.register(fd, select.POLLIN)
        deadline = time.monotonic() + timeout if timeout > 0 else float("inf")

        while time.monotonic() < deadline:
            if cancel_event and cancel_event.is_set():
                self._kill_process_group()
                output = output_buf.decode("utf-8", errors="replace").strip()
                full_len = len(output)
                if len(output) > _MAX_BASH_OUTPUT_CHARS:
                    truncated = output[:_MAX_BASH_OUTPUT_CHARS]
                    saved_msg = _save_truncated_output(output, command)
                    output = truncated + f"\n...（输出已截断，共 {full_len} 字符）{saved_msg}"
                return {"output": output, "exit_code": -1, "timed_out": False}

            if process.poll() is not None and not sentinel_found:
                remaining = os.read(fd, 65536)
                if remaining:
                    output_buf.extend(remaining)
                break

            events = poll.poll(50)
            if not events:
                continue

            chunk = os.read(fd, 65536)
            if not chunk:
                break

            output_buf.extend(chunk)

            sidx = output_buf.find(self._SENTINEL_B)
            if sidx != -1:
                sentinel_found = True
                after = output_buf[sidx:]
                eidx = after.find(self._SENTINEL_END_B)
                if eidx != -1:
                    code_bytes = after[len(self._SENTINEL_B):eidx]
                    try:
                        exit_code = int(code_bytes.decode("ascii"))
                    except (ValueError, UnicodeDecodeError):
                        exit_code = -1
                output_buf = output_buf[:sidx]
                break

        if not sentinel_found:
            self._timed_out = True
            self._kill_process_group()
            output = output_buf.decode("utf-8", errors="replace").strip()
            full_len = len(output)
            if len(output) > _MAX_BASH_OUTPUT_CHARS:
                truncated = output[:_MAX_BASH_OUTPUT_CHARS]
                saved_msg = _save_truncated_output(output, command)
                output = truncated + f"\n...（输出已截断，共 {full_len} 字符）{saved_msg}"
            return {
                "output": f"命令执行超时（{timeout}秒），session 已终止。\n最后输出：{output[:600]}",
                "exit_code": -1,
                "timed_out": True,
            }

        output = output_buf.decode("utf-8", errors="replace").strip()
        full_len = len(output)
        if len(output) > _MAX_BASH_OUTPUT_CHARS:
            truncated = output[:_MAX_BASH_OUTPUT_CHARS]
            saved_msg = _save_truncated_output(output, command)
            output = truncated + f"\n...（输出已截断，共 {full_len} 字符）{saved_msg}"

        return {"output": output, "exit_code": exit_code, "timed_out": False}

    def _kill_process_group(self):
        if self.process is not None and self.process.pid is not None:
            try:
                os.killpg(os.getpgid(self.process.pid), 9)
            except (ProcessLookupError, PermissionError, OSError):
                try:
                    self.process.kill()
                except OSError:
                    pass

    def stop(self):
        if self.process is None:
            return
        try:
            self.process.terminate()
            self.process.wait(timeout=5)
        except (subprocess.TimeoutExpired, ProcessLookupError, OSError):
            self._kill_process_group()
        self._started = False
        self.process = None

    def restart(self):
        self.stop()
        self._timed_out = False
        self.start()


def set_bash_cwd(path: str) -> str:
    """Set the working directory for the bash session."""
    import shlex
    path = os.path.abspath(os.path.expanduser(path.strip()))
    if not os.path.exists(path):
        return f"❌ 路径不存在：{path}"
    if not os.path.isdir(path):
        return f"❌ 路径不是一个目录：{path}"

    _bash_state.cwd = path
    if _bash_state.session is not None and _bash_state.session._started and _bash_state.session.process is not None:
        if _bash_state.session.process.poll() is None:
            try:
                cmd = f"cd {shlex.quote(path)}"
                res = _bash_state.session.execute(cmd, timeout=5)
                if res.get("timed_out", False):
                    return f"⚠️ 目录切换命令超时，已更新全局配置。新路径：{path}"
            except Exception as e:
                return f"⚠️ 现有 Bash 会话异常（{e}），已更新工作目录配置。新路径：{path}"
    return f"✅ Bash 工作路径已切换至：{path}"


def get_bash_cwd() -> str:
    """Get the current working directory of the bash session."""
    return _bash_state.cwd


def execute_bash(command: str, restart: bool = False,
                 timeout: int = _BASH_TIMEOUT_DEFAULT, cancel_event=None) -> str:
    """Execute a shell command in a persistent bash session."""
    if not command or not command.strip():
        return "错误：命令不能为空。"

    interactive_err = _check_interactive(command)
    if interactive_err is not None:
        return interactive_err

    if restart:
        if _bash_state.session is not None:
            _bash_state.session.stop()
        _bash_state.session = _BashSession()
        _bash_state.session.start()
        if not command or not command.strip():
            return "🔄 Bash session 已重启。"

    timeout = max(1, min(120, timeout))

    if _bash_state.session is None:
        _bash_state.session = _BashSession()
        _bash_state.session.start()

    try:
        result = _bash_state.session.execute(command, timeout=timeout, cancel_event=cancel_event)
    except RuntimeError:
        if _bash_state.session is not None:
            _bash_state.session.stop()
        _bash_state.session = _BashSession()
        _bash_state.session.start()
        try:
            result = _bash_state.session.execute(command, timeout=timeout, cancel_event=cancel_event)
        except RuntimeError as e:
            return f"错误：{e}"

    output = result.get("output", "")
    exit_code = result.get("exit_code", -1)
    timed_out = result.get("timed_out", False)

    if timed_out:
        if _bash_state.session is not None:
            _bash_state.session.stop()
        _bash_state.session = _BashSession()
        _bash_state.session.start()
        parts = ["⚠️ 命令执行超时，已自动重启 bash session"]
        if output:
            parts.append("")
            parts.append(output)
        return "\n".join(parts)

    status_icon = "✅" if exit_code == 0 else "⚠️" if exit_code == -1 else "❌"
    parts = [f"{status_icon} 命令执行完成（退出码：{exit_code}）"]
    if output:
        parts.append("")
        parts.append(output)

    return "\n".join(parts)


TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "bash",
            "description": "执行 shell 命令。使用持久化 bash 会话，命令之间的工作目录和上下文不重置。自动检测并阻止交互式命令。超时会自动重启会话。",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "要执行的 shell 命令"},
                    "restart": {"type": "boolean", "description": "是否重启 bash 会话后再执行", "default": False},
                    "timeout": {"type": "integer", "description": "命令超时秒数（1-120，默认 60）", "default": 60}
                },
                "required": ["command"]
            }
        }
    },
]
