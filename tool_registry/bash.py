"""Bash execution tool — persistent bash session with command execution."""

import os
import re
import select
import shlex
import subprocess
import tempfile
import time
import uuid
from typing import Final, Optional

from ._state import bash as _bash_state


_MAX_BASH_OUTPUT_CHARS = 20000
_BASH_TIMEOUT_DEFAULT = 120
_BASH_SHELL = "/bin/bash"

_HARD_BLOCK: Final[frozenset] = frozenset({
    "vi", "vim", "nvim", "nano", "emacs", "vimdiff",
    "less", "more", "most",
    "top", "htop", "btop", "iftop", "iotop",
    "ssh", "telnet", "rlogin", "sftp",
    "screen", "tmux",
    "watch",
})

_BARE_REPL: Final[frozenset] = frozenset({
    "python", "python3", "ipython",
    "node", "irb",
    "bash", "zsh", "sh", "dash", "fish",
    "mysql", "mariadb", "psql", "sqlite3",
    "redis-cli", "mongo", "mongosh",
    "clickhouse-client", "cqlsh", "cockroach",
    "bc", "dc",
})

_CONDITIONAL: Final[dict] = {
    "ssh-keygen": {
        "safe_flags": ("-b", "-f", "-t"),
        "message": "请提供 -b 或 -f 参数避免交互式提问",
    },
    "openssl": {
        "safe_flags": ("-subj", "-pass", "-k"),
        "safe_subcommands": ("version", "help", "enc", "dgst", "rand",
                             "speed", "prime", "genrsa", "genpkey",
                             "x509", "pkcs12", "pkey",
                             "verify", "list", "ciphers", "ecparam"),
        "message": "请提供 -subj（证书请求）或 -pass（加密）参数避免交互",
    },
    "gpg": {
        "safe_flags": ("--batch",),
        "message": "建议使用 --batch 参数避免交互",
    },
    "adduser": {
        "safe_flags": (),
        "message": "该命令无法在非交互模式下运行，请直接使用 useradd 并指定完整参数",
    },
    "useradd": {
        "safe_flags": (),
        "message": "请提供 -m -s -u 等完整参数避免交互",
    },
    "passwd": {
        "safe_flags": (),
        "message": "该命令无法在非交互模式下运行",
    },
    "htpasswd": {
        "safe_flags": ("-b",),
        "message": "请使用 -b 参数提供密码避免交互",
    },
}


def _check_interactive(command: str) -> Optional[str]:
    """Check if command is interactive and should be blocked.

    Three severity levels:
      1. _HARD_BLOCK — always blocked (needs TTY)
      2. _BARE_REPL  — blocked only when run without arguments
      3. _CONDITIONAL — blocked when safe flags are absent
    """
    if not command or not command.strip():
        return None
    parts = command.strip().split(maxsplit=1)
    first_word = parts[0].strip()
    has_args = len(parts) > 1 and bool(parts[1].strip())

    if first_word in _HARD_BLOCK:
        return (f"错误：不支持交互式命令「{first_word}」。\n"
                f"   该命令需要 TTY 终端，无法在后台管道模式下执行。")

    if first_word in _BARE_REPL and not has_args:
        return (f"错误：裸启动「{first_word}」会进入交互模式。\n"
                f"   如需执行脚本，请提供参数，例如: {first_word} script.py")

    if first_word in _CONDITIONAL:
        entry = _CONDITIONAL[first_word]
        safe_flags = entry.get("safe_flags", ())
        safe_subcmds = entry.get("safe_subcommands", ())

        # If command has a subcommand that is known safe, let it through
        if safe_subcmds and len(parts) > 1:
            subcmd = parts[1].strip().split(maxsplit=1)[0]
            if subcmd in safe_subcmds:
                return None

        # Check whether safe flags are present (token-level match)
        if safe_flags:
            args_part = parts[1] if len(parts) > 1 else ""
            if has_args:
                try:
                    args_tokens = shlex.split(args_part)
                except ValueError:
                    args_tokens = args_part.split()
                if any(flag in args_tokens for flag in safe_flags):
                    return None
            return (f"⚠️ 警告：检测到可能交互的命令「{first_word}」。\n"
                    f"   {entry['message']}")

        # No safe_flags defined — always intercept
        return (f"⚠️ 警告：检测到可能交互的命令「{first_word}」。\n"
                f"   {entry['message']}")

    return None


_HARDENED_ENV: Final[dict] = {
    # Disable pagers — prevents `less`/`more` from hanging on TTY-less pipe
    "PAGER": "cat",
    "GIT_PAGER": "cat",
    "MANPAGER": "cat",
    "SYSTEMD_PAGER": "cat",
    "LESS": "FRXMK",
    # Suppress editor prompts — prevents git/others from waiting for editor
    "EDITOR": "true",
    "GIT_EDITOR": "true",
    "GIT_SEQUENCE_EDITOR": "true",
    "GIT_MERGE_AUTOEDIT": "no",
    # Suppress interactive auth prompts
    "GIT_TERMINAL_PROMPT": "0",
    "SSH_ASKPASS": "/usr/bin/false",
    "SSH_ASKPASS_REQUIRE": "never",
    # Prevent package managers from prompting
    "DEBIAN_FRONTEND": "noninteractive",
    "APT_LISTCHANGES_FRONTEND": "none",
    "NEEDRESTART_MODE": "a",
    # Mark as non-interactive CI context
    "CI": "1",
    "TERM": "dumb",
}


_SESSION_BREAKER_PATTERNS = [
    (r'^\s*exit\b', "exit 命令会终止 bash 会话，请直接继续执行下一条命令，工具会自动重启会话"),
    (r'\bkill\s+\$\$', "kill $$ 会终止 bash 会话，请使用 restart=True 代替"),
    (r'^\s*exec\b', "exec 命令会替换当前 shell 进程，可能导致会话中断"),
]


def _check_session_breaker(command: str) -> Optional[str]:
    """检查命令是否可能破坏当前的持久化 bash 会话。"""
    if not command or not command.strip():
        return None
    for pattern, msg in _SESSION_BREAKER_PATTERNS:
        if re.search(pattern, command):
            return f"⚠️ 检测到可能中断会话的命令：{msg}"
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

    _process: Optional["subprocess.Popen[bytes]"] = None

    _MAX_AUTO_RECOVERIES = 3

    def __init__(self):
        self.process: Optional["subprocess.Popen[bytes]"] = None
        self._timed_out = False
        self._started = False
        self._auto_recover_count = 0

    def start(self, cwd: Optional[str] = None):
        """Spawn a new persistent bash subprocess (binary pipe mode)."""
        merged_env = {**os.environ, **_HARDENED_ENV}
        self.process = subprocess.Popen(
            [_BASH_SHELL],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
            cwd=cwd or _bash_state.cwd,
            env=merged_env,
            preexec_fn=os.setsid if hasattr(os, "setsid") else None,
        )
        self._started = True
        self._auto_recover_count = 0
        self._timed_out = False

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
            if self._auto_recover_count >= self._MAX_AUTO_RECOVERIES:
                return {"output": "错误：Bash 进程已意外退出（自动恢复失败，超过最大重试次数）。\n💡 提示：请使用 restart=True 重置会话。", "exit_code": process.returncode, "timed_out": False}
            self._auto_recover_count += 1
            self.start()
            return self.execute(command, timeout=timeout, cancel_event=cancel_event)

        sentinel_id = uuid.uuid4().hex[:12]
        sentinel_start = b"BSEP_" + sentinel_id.encode() + b"_S"
        sentinel_end = b"BSEP_" + sentinel_id.encode() + b"_E"

        sentinel_cmd_b = (
            b"{ " + command.encode("utf-8", errors="replace")
            + b"; } 2>&1; echo "
            + sentinel_start + b"$?" + sentinel_end + b"\n"
        )

        try:
            process.stdin.write(sentinel_cmd_b)
            process.stdin.flush()
        except BrokenPipeError:
            return {"output": "错误：Bash 进程已关闭（stdin 写入失败）。\n💡 提示：请使用 restart=True 重置会话。", "exit_code": -1, "timed_out": False}

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

            sidx = output_buf.find(sentinel_start)
            if sidx != -1:
                sentinel_found = True
                after = output_buf[sidx:]
                eidx = after.find(sentinel_end)
                if eidx != -1:
                    code_bytes = after[len(sentinel_start):eidx]
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
                "output": f"命令执行超时（{timeout}秒），session 已终止。\n{output}",
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
        self._auto_recover_count = 0
        self.start()


def _resolve_session_key(session_key: Optional[str] = None) -> str:
    """Resolve session_key from explicit param, or from conversation context, or default."""
    if session_key is not None:
        return session_key
    from .subagent import get_current_conversation_id
    conv_id = get_current_conversation_id()
    return conv_id if conv_id else "default"


def _get_session(session_key: str = "default") -> _BashSession:
    """Get or create the bash session for the given key."""
    if session_key not in _bash_state._sessions:
        session = _BashSession()
        cwd = _bash_state.get_cwd(session_key)
        session.start(cwd=cwd)
        _bash_state._sessions[session_key] = session
        _bash_state._cwds.setdefault(session_key, cwd)
    return _bash_state._sessions[session_key]


def close_bash_session(session_key: str):
    """Kill the bash process for a given session key and remove it."""
    if session_key == "default":
        return  # never close the default session
    session = _bash_state._sessions.pop(session_key, None)
    if session:
        session.stop()


def set_bash_cwd(path: str, session_key: Optional[str] = None) -> str:
    """Set the working directory for the bash session (or a specific session)."""
    path = os.path.abspath(os.path.expanduser(path.strip()))
    if not os.path.exists(path):
        return f"❌ 路径不存在：{path}"
    if not os.path.isdir(path):
        return f"❌ 路径不是一个目录：{path}"

    key = _resolve_session_key(session_key)
    _bash_state.set_cwd(key, path)
    session = _bash_state._sessions.get(key)
    if session is not None and session._started and session.process is not None:
        if session.process.poll() is None:
            try:
                cmd = f"cd {shlex.quote(path)}"
                res = session.execute(cmd, timeout=5)
                if res.get("timed_out", False):
                    return f"⚠️ 目录切换命令超时，已更新全局配置。新路径：{path}"
            except Exception as e:
                return f"⚠️ 现有 Bash 会话异常（{e}），已更新工作目录配置。新路径：{path}"
    return f"✅ Bash 工作路径已切换至：{path}"


def get_bash_cwd(session_key: Optional[str] = None) -> str:
    """Get the current working directory for the bash session (or a specific session)."""
    key = _resolve_session_key(session_key)
    return _bash_state.get_cwd(key)


_EXIT_CODE_HINTS = {
    0:    "",
    1:    "通用错误",
    126:  "权限不足（Permission denied）",
    127:  "命令未找到（command not found）",
    128:  "退出信号",
    130:  "被 Ctrl+C 中断",
    -1:   "内部错误（进程异常退出）",
}


def _exit_code_hint(code: int) -> str:
    return _EXIT_CODE_HINTS.get(code, f"未知退出码 {code}")


def execute_bash(command: str, restart: bool = False,
                 timeout: int = _BASH_TIMEOUT_DEFAULT,
                 max_chars: int = _MAX_BASH_OUTPUT_CHARS,
                 cancel_event=None) -> str:
    """Execute a shell command in a persistent bash session."""
    if not command or not command.strip():
        return "错误：命令不能为空。"

    interactive_err = _check_interactive(command)
    if interactive_err is not None:
        return interactive_err

    breaker_err = _check_session_breaker(command)
    if breaker_err is not None:
        return breaker_err

    session_key = _resolve_session_key()

    if restart:
        close_bash_session(session_key)
        _bash_state._sessions.pop(session_key, None)
        _get_session(session_key)

    timeout = max(1, min(120, timeout))
    max_chars = max(500, min(_MAX_BASH_OUTPUT_CHARS, max_chars))

    session = _get_session(session_key)

    try:
        result = session.execute(command, timeout=timeout, cancel_event=cancel_event)
    except RuntimeError:
        close_bash_session(session_key)
        _bash_state._sessions.pop(session_key, None)
        session = _get_session(session_key)
        try:
            result = session.execute(command, timeout=timeout, cancel_event=cancel_event)
        except RuntimeError as e:
            return f"错误：{e}"

    output = result.get("output", "")
    exit_code = result.get("exit_code", -1)
    timed_out = result.get("timed_out", False)

    # Apply max_chars truncation in execute_bash layer
    if len(output) > max_chars:
        output = output[:max_chars] + f"\n\n...（输出已截断，共 {len(output)} 字符）"

    if timed_out:
        close_bash_session(session_key)
        parts = ["⚠️ 命令执行超时，已自动重启 bash session"]
        if output:
            parts.append("")
            parts.append(output)
        return "\n".join(parts)

    status_icon = "✅" if exit_code == 0 else "⚠️" if exit_code == -1 else "❌"
    hint = _exit_code_hint(exit_code)
    exit_str = f"（退出码：{exit_code}）" if not hint else f"（退出码：{exit_code} — {hint}）"
    parts = [f"{status_icon} 命令执行完成{exit_str}"]
    if output:
        parts.append("")
        parts.append(output)

    return "\n".join(parts)


def execute_bash_get_session_info(session_key: Optional[str] = None) -> str:
    """获取当前对话的 Bash 会话状态信息。"""
    key = _resolve_session_key(session_key)
    session = _bash_state._sessions.get(key)
    parts = [f"📋 Bash 会话信息"]
    if session is not None and session._started and session.process is not None and session.process.poll() is None:
        parts.append(f"  会话状态: 活跃")
        parts.append(f"  进程 PID: {session.process.pid}")
        parts.append("")
        parts.append("💡 如有必要请使用 `kill <PID>` 精确终止此会话，")
        parts.append("   切勿使用 pkill -9 -f \"bash\" 或 killall bash，")
        parts.append("   否则会误杀系统上其他 bash 进程。")
    else:
        parts.append(f"  会话状态: 未启动/已终止")
    return "\n".join(parts)


TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "bash",
            "description": "执行 shell 命令。使用持久化 bash 会话，命令之间的工作目录和上下文不重置。自动检测并阻止交互式命令（编辑器、REPL、数据库客户端、网络工具等），环境已预硬化（禁用翻页器/交互提示）。超时会自动重启会话。",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "要执行的 shell 命令"},
                    "restart": {"type": "boolean", "description": "是否重启 bash 会话后再执行", "default": False},
                    "timeout": {"type": "integer", "description": "命令超时秒数（1-120，默认 120）", "default": 120},
                    "max_chars": {"type": "integer", "description": "输出最大字符数（500-20000，默认 20000）", "default": 20000}
                },
                "required": ["command"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "bash_get_session_info",
            "description": "获取当前 Bash 会话的状态信息，包括进程 PID 和会话是否活跃。如需获取工作目录请执行 pwd 命令。返回信息包含安全提示：如需终止会话应使用 kill <PID> 而非 pkill/pkill -f，防止误杀。",
            "parameters": {
                "type": "object",
                "properties": {}
            }
        }
    },
]
