"""File system tools — list, read, write, edit, delete, rename files and get file info."""

import datetime
import difflib
import math
import os
import shutil
import stat
import tempfile
from typing import Any, Dict, Final, List, Optional, Tuple

from ._state import file_read
# Shared dict reference — subagent.py saves/restores file_read.store,
# and this alias ensures both modules operate on the SAME dict object.
_READ_FILE_STATE = file_read.store


def _check_file_stale(path: str, mode: str = "string") -> Tuple[Optional[str], bool]:
    """Check if file has been modified since read_file was called.

    Returns (error_msg, was_externally_modified):
        error_msg is None if OK, error string if stale/missing.
        was_externally_modified is True when mtime changed but content is identical.
    """
    resolved = os.path.realpath(path)
    state = _READ_FILE_STATE.get(resolved)
    if state is None:
        return f"错误：文件「{path}」尚未被读取。请先使用 read_file 工具读取该文件。", False
    # string 模式：有 old_string 兜底，存在读取记录即可
    # line 模式：无字符串匹配兜底，必须完整读取
    if mode == "line" and not state.get("full_read", False):
        return f"错误：文件「{path}」之前只读取了部分内容。line 模式下请使用 read_file 完整读取后再编辑。", False
    try:
        current_mtime = os.path.getmtime(resolved)
    except OSError:
        return f"错误：无法访问文件「{path}」", False
    if current_mtime > state["mtime"]:
        try:
            with open(resolved, "rb") as f:
                raw = f.read()
            current_content = raw.decode(state.get("encoding", "utf-8"), errors="replace")
        except Exception:
            return f"错误：文件「{path}」自读取后已被修改，请重新读取。", False
        if current_content != state["content"]:
            return f"错误：文件「{path}」自读取后已被外部修改，请重新使用 read_file 读取。", False
        state["mtime"] = current_mtime
        return None, True
    return None, False


def _resolve_safe_path(path: str) -> Optional[str]:
    """Resolve a path safely. Returns the resolved absolute path if it exists, None otherwise."""
    if not path or not isinstance(path, str) or not os.path.isabs(path):
        return None
    resolved = os.path.realpath(path)
    if not os.path.exists(resolved):
        return None
    return resolved


def _resolve_write_path(path: str, force: bool = False) -> Optional[str]:
    """Resolve a path for write operations. Does NOT require the file to exist."""
    if not path or not isinstance(path, str) or not os.path.isabs(path):
        return None
    resolved = os.path.realpath(path)
    parent = os.path.dirname(resolved)
    if not os.path.isdir(parent):
        return None
    if os.path.exists(resolved) and not force:
        return None
    return resolved


_FILE_SIZE_UNITS = ["B", "KB", "MB", "GB", "TB"]


def _format_file_size(size_bytes: int) -> str:
    if size_bytes == 0:
        return "0 B"
    unit_idx = int(math.floor(math.log(size_bytes, 1024)))
    unit_idx = min(unit_idx, len(_FILE_SIZE_UNITS) - 1)
    value = size_bytes / (1024 ** unit_idx)
    if unit_idx == 0:
        return f"{int(value)} B"
    return f"{value:.1f} {_FILE_SIZE_UNITS[unit_idx]}"


def _atomic_write(path: str, content: str, line_ending: str = "LF") -> None:
    """Atomically write content to file via temp file + rename."""
    resolved = os.path.realpath(path)
    tmp_fd, tmp_path = tempfile.mkstemp(
        dir=os.path.dirname(resolved),
        prefix=f'.{os.path.basename(resolved)}.',
        suffix='.tmp'
    )
    try:
        with os.fdopen(tmp_fd, 'w', encoding='utf-8',
                       newline=('\r\n' if line_ending == 'CRLF' else '\n')) as f:
            f.write(content)
        os.replace(tmp_path, resolved)
    except Exception:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise


def _generate_diff(old_content: str, new_content: str, path: str, n: int = 2) -> str:
    """Generate unified diff preview, max 30 lines."""
    basename = os.path.basename(path)
    diff_lines = list(difflib.unified_diff(
        old_content.splitlines(keepends=True),
        new_content.splitlines(keepends=True),
        fromfile=f'a/{basename}',
        tofile=f'b/{basename}',
        n=n,
    ))
    if not diff_lines:
        return ""
    if len(diff_lines) > 30:
        diff_lines = diff_lines[:30] + [f"... (共 {len(diff_lines) - 30} 行已省略)\n"]
    return ''.join(diff_lines)


def _find_line_numbers(content: str, old_string: str) -> List[int]:
    """Find all line numbers where old_string appears."""
    lines = []
    start = 0
    while True:
        idx = content.find(old_string, start)
        if idx == -1:
            break
        line_num = content[:idx].count('\n') + 1
        lines.append(line_num)
        start = idx + 1
    return lines


_MAX_DIRECTORY_LISTING = 200


def execute_list_directory(path: str, include_hidden: bool = False,
                           sort_by: str = "name", reverse: bool = False) -> str:
    """List contents of a directory. Accepts absolute paths only."""
    resolved = _resolve_safe_path(path)
    if resolved is None:
        return f"错误：目录不存在或路径无效「{path}」"
    if not os.path.isdir(resolved):
        return f"错误：路径不是目录「{resolved}」"
    try:
        raw_names = os.listdir(resolved)
    except PermissionError:
        return f"错误：无权访问目录「{resolved}」"
    except OSError as e:
        return f"错误：访问目录时出错「{resolved}」: {e}"

    entries = []
    for name in raw_names:
        if not include_hidden and name.startswith("."):
            continue
        full = os.path.join(resolved, name)
        try:
            st = os.lstat(full)
            is_dir = stat.S_ISDIR(st.st_mode)
            is_link = os.path.islink(full)
            entries.append((name, full, st, is_dir, is_link))
        except OSError:
            entries.append((name, full, None, False, False))

    if sort_by == "name":
        entries.sort(key=lambda e: (0 if e[3] else 1, e[0].lower()))
    elif sort_by == "size":
        entries.sort(key=lambda e: (0 if e[3] else 1, -(e[2].st_size if e[2] is not None and not e[3] else 0)))
    elif sort_by == "time":
        entries.sort(key=lambda e: (0 if e[3] else 1, -(e[2].st_mtime if e[2] is not None else 0)))

    if reverse:
        entries.reverse()

    lines = []
    for name, full, st, is_dir, is_link in entries:
        try:
            if is_link:
                marker = "LINK"
                target = os.readlink(full)
                name_display = f"{name} → {target}"
            elif is_dir:
                marker = "DIR"
                name_display = name
            else:
                marker = "FILE"
                name_display = name
            if st is not None and not is_dir:
                size = _format_file_size(st.st_size)
            else:
                size = "—"
            if st is not None:
                mtime = datetime.datetime.fromtimestamp(st.st_mtime).strftime("%m-%d %H:%M")
            else:
                mtime = "?"
        except OSError:
            marker = "?"
            size = "?"
            mtime = "?"
            name_display = name

        lines.append(f"[{marker:4s}] {size:>8s}  {mtime}  {name_display}")

    if not lines:
        return f"目录「{resolved}」为空。"

    total = len(lines)
    if total > _MAX_DIRECTORY_LISTING:
        lines = lines[:_MAX_DIRECTORY_LISTING]
        lines.append(f"\n...（已截断，仅显示前 {_MAX_DIRECTORY_LISTING} 项，共 {total} 项）")

    return f"📁 目录列表: {resolved}\n\n" + "\n".join(lines)


def execute_read_file(path: str, max_chars: int = 20000, start_line: int = 1,
                      end_line: Optional[int] = None,
                      purpose: str = "") -> str:
    """Read a text file's content. Accepts absolute paths only, with optional line range."""
    resolved = _resolve_safe_path(path)
    if resolved is None:
        return f"错误：文件不存在或路径无效「{path}」"
    if not os.path.isfile(resolved):
        return f"错误：路径不是文件「{resolved}」"

    max_chars = max(500, min(200000, max_chars))

    if start_line < 1:
        start_line = 1
    if end_line is not None and end_line < start_line:
        return f"错误：结束行号「{end_line}」不能小于起始行号「{start_line}」"

    try:
        with open(resolved, "rb") as f:
            header = f.read(8192)
    except PermissionError:
        return f"错误：无权读取文件「{resolved}」"
    except OSError as e:
        return f"错误：读取文件时出错「{resolved}」: {e}"

    if b"\x00" in header:
        return f"错误：文件「{resolved}」是二进制文件（或包含 null 字节），不支持读取。"

    try:
        with open(resolved, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except UnicodeDecodeError:
        return f"错误：文件「{resolved}」不是有效的 UTF-8 文本文件。"
    except PermissionError:
        return f"错误：无权读取文件「{resolved}」"
    except OSError as e:
        return f"错误：读取文件时出错「{resolved}」: {e}"

    total_lines = len(lines)
    start_idx = start_line - 1
    if start_idx >= total_lines:
        return f"提示：文件「{resolved}」共有 {total_lines} 行，起始行号「{start_line}」超出了文件范围。"

    end_idx = total_lines if end_line is None else min(end_line, total_lines)
    sliced_lines = lines[start_idx:end_idx]
    content = "".join(sliced_lines)
    # 预计算完整文件信息（供截断消息、头部块、状态块共用，消除冗余计算）
    full_content = "".join(lines)
    total_chars = len(full_content)
    file_size_str = _format_file_size(os.path.getsize(resolved))
    truncated_by_chars = False

    if end_line is None and len(content) > max_chars:
        content = content[:max_chars]
        truncated_by_chars = True

    is_full_read = (start_line == 1 and end_line is None and not truncated_by_chars)

    if truncated_by_chars:
        pct = round(len(content) / total_chars * 100) if total_chars else 0
        content += f"\n\n...（内容超出 max_chars={max_chars} 字符，文件共 {total_lines} 行 / {file_size_str}，已读 {pct}%）"
    elif end_line is not None and end_line < total_lines:
        content += f"\n\n...（已截断，仅显示第 {start_line} 至 {end_line} 行，文件共 {total_lines} 行）"
    elif start_line > 1:
        content += f"\n\n...（已截断，仅显示第 {start_line} 行至文件末尾，文件共 {total_lines} 行）"

    if start_line == 1:
        try:
            line_ending = "CRLF" if "\r\n" in full_content else "LF"
            header = (
                f"--- {os.path.basename(resolved)} ({file_size_str}, {total_lines} 行, utf-8, {line_ending})\n"
                f"--- {resolved}\n"
                + ("-" * 44) + "\n\n"
            )
            content = header + content
        except OSError:
            pass

    # 无论是否完整读取，都写入状态（部分读取记录 full_read=False）
    try:
        line_ending = "CRLF" if "\r\n" in full_content else "LF"
        _READ_FILE_STATE[resolved] = {
            "content": full_content,
            "mtime": os.path.getmtime(resolved),
            "full_read": is_full_read,
            "encoding": "utf-8",
            "line_ending": line_ending,
        }
    except OSError:
        pass

    return content


def execute_edit_file(path: str, old_string: str = "", new_string: str = "",
                      replace_all: bool = False, mode: str = "string",
                      start_line: Optional[int] = None,
                      end_line: Optional[int] = None,
                      force: bool = False,
                      purpose: str = "") -> str:
    """Edit a file via string replacement or line-range replacement."""
    resolved = _resolve_safe_path(path)
    if resolved is None:
        return f"错误：文件不存在或路径无效「{path}」"
    if not os.path.isfile(resolved):
        return f"错误：路径不是文件「{resolved}」"

    if not force:
        stale_err, was_modified = _check_file_stale(resolved, mode=mode)
        if stale_err is not None:
            return stale_err
    else:
        was_modified = False

    ext_warn = "\n⚠️ 文件自读取后已被外部修改（内容一致，已刷新状态）。" if was_modified else ""

    if mode not in ("string", "line"):
        return "错误：mode 必须是 'string' 或 'line'。"

    state = _READ_FILE_STATE.get(os.path.realpath(resolved))
    line_ending = state.get("line_ending", "LF") if state else "LF"

    try:
        with open(resolved, "r", encoding="utf-8") as f:
            content = f.read()
    except UnicodeDecodeError:
        return f"错误：文件「{resolved}」不是有效的 UTF-8 文本文件。"
    except PermissionError:
        return f"错误：无权读取文件「{resolved}」"
    except OSError as e:
        return f"错误：读取文件时出错「{resolved}」: {e}"

    if mode == "line":
        lines = content.splitlines(keepends=True)
        if start_line is None:
            return "错误：line 模式下必须提供 start_line。"
        if start_line < 1 or start_line > len(lines):
            return f"错误：start_line {start_line} 超出文件范围（共 {len(lines)} 行）。"
        if end_line is not None:
            if end_line < start_line:
                return f"错误：end_line（{end_line}）不能小于 start_line（{start_line}）。"
            if end_line > len(lines):
                return f"错误：end_line {end_line} 超出文件范围（共 {len(lines)} 行）。"
        else:
            end_line = start_line

        removed_lines = lines[start_line - 1:end_line]
        trailing_ending = removed_lines[-1] if removed_lines else "\n"
        if trailing_ending and not trailing_ending.endswith("\n"):
            trailing_ending += "\n"

        if new_string and not new_string.endswith("\n"):
            new_string += "\n"

        new_lines = lines[:start_line - 1] + [new_string] + lines[end_line:]
        new_content = "".join(new_lines)
        actual_changes = 1

        diff = _generate_diff(content, new_content, path)
        diff_block = f"\n{diff}" if diff else ""
        try:
            _atomic_write(resolved, new_content, line_ending)
        except PermissionError:
            return f"错误：无权写入文件「{path}」"
        except OSError as e:
            return f"错误：写入文件时出错「{path}」: {e}"

        _READ_FILE_STATE[os.path.realpath(resolved)] = {
            "content": new_content,
            "mtime": os.path.getmtime(resolved),
            "full_read": True,
            "encoding": "utf-8",
            "line_ending": line_ending,
        }
        return f"✅ 已编辑文件「{path}」\n   模式: line（L{start_line}-L{end_line}）\n   变更: {actual_changes} 处替换{diff_block}{ext_warn}"

    if not old_string:
        return "错误：string 模式下 old_string 不能为空。"

    if old_string not in content:
        return (
            f"错误：未能在文件「{path}」中找到指定的 old_string。\n\n"
            f"请确保 old_string 与文件内容完全匹配（包括空格和缩进）。\n"
            f"如需查看文件当前内容，请使用 read_file 读取。"
        )

    occurrence_count = content.count(old_string)
    if occurrence_count > 1 and not replace_all:
        line_nums = _find_line_numbers(content, old_string)
        lines_str = ", ".join(f"第 {n} 行" for n in line_nums)
        return (
            f"错误：old_string 在文件中出现了 {occurrence_count} 次\n"
            f"   位置: {lines_str}\n"
            f"   建议: 设置 replace_all=True 替换全部，或提供更多上下文以唯一匹配"
        )

    new_content = content.replace(old_string, new_string) if replace_all else content.replace(old_string, new_string, 1)
    actual_changes = occurrence_count if replace_all else 1

    try:
        _atomic_write(resolved, new_content, line_ending)
    except PermissionError:
        return f"错误：无权写入文件「{path}」"
    except OSError as e:
        return f"错误：写入文件时出错「{path}」: {e}"

    _READ_FILE_STATE[os.path.realpath(resolved)] = {
        "content": new_content,
        "mtime": os.path.getmtime(resolved),
        "full_read": True,
        "encoding": "utf-8",
        "line_ending": line_ending,
    }

    diff = _generate_diff(content, new_content, path)
    diff_block = f"\n{diff}" if diff else ""
    return f"✅ 已编辑文件「{path}」\n   变更: {actual_changes} 处替换{diff_block}{ext_warn}"


def execute_file_info(path: str) -> str:
    """Get file/directory metadata: size, mtime, atime, permissions, type, owner."""
    raw_path = os.path.expanduser(path)
    is_symlink = os.path.islink(raw_path)
    link_target = os.readlink(raw_path) if is_symlink else None

    resolved = _resolve_safe_path(path)
    if resolved is None:
        if is_symlink:
            return (
                f"📋 文件信息: {raw_path}\n"
                f"  类型: 符号链接（破损）→ {link_target}\n"
                f"  大小: —（目标不存在）"
            )
        return f"错误：文件或目录不存在或路径无效「{path}」"

    try:
        st = os.stat(resolved)
    except PermissionError:
        return f"错误：无权访问「{resolved}」"
    except OSError as e:
        return f"错误：访问「{resolved}」时出错: {e}"

    if is_symlink:
        file_type = f"符号链接 → {link_target}"
    elif os.path.isdir(resolved):
        file_type = "目录"
    elif os.path.isfile(resolved):
        file_type = "文件"
    else:
        file_type = "其他"

    mode = st.st_mode
    perm_octal = oct(stat.S_IMODE(mode))[2:]
    perm_str = stat.filemode(mode)

    try:
        import pwd
        import grp
        owner = pwd.getpwuid(st.st_uid).pw_name
        group = grp.getgrgid(st.st_gid).gr_name
    except (ImportError, KeyError):
        owner = str(st.st_uid)
        group = str(st.st_gid)

    if os.path.isdir(resolved) and not is_symlink:
        size_str = "—（目录）"
    else:
        size_str = _format_file_size(st.st_size)

    local_tz = datetime.datetime.now().astimezone().tzinfo
    mtime = datetime.datetime.fromtimestamp(st.st_mtime, tz=local_tz).strftime("%Y-%m-%d %H:%M:%S")
    atime = datetime.datetime.fromtimestamp(st.st_atime, tz=local_tz).strftime("%Y-%m-%d %H:%M:%S")
    ctime = datetime.datetime.fromtimestamp(st.st_ctime, tz=local_tz).strftime("%Y-%m-%d %H:%M:%S")

    lines = [
        f"📋 文件信息: {resolved}",
        f"  类型: {file_type}",
        f"  大小: {size_str}",
        f"  权限: {perm_octal} ({perm_str})",
        f"  所有者: {owner}:{group}",
        f"  修改时间: {mtime}",
        f"  访问时间: {atime}",
        f"  创建时间: {ctime}",
    ]

    return "\n".join(lines)


def execute_write_file(path: str, content: str, force: bool = False,
                       mode: str = "write",
                       purpose: str = "") -> str:
    """Create a new file or overwrite an existing file's content."""
    if not path or not isinstance(path, str) or not os.path.isabs(path):
        return "错误：必须使用绝对路径！"

    _MAX_WRITE_CHARS = 100000

    if mode == "append":
        resolved = os.path.realpath(path)
        parent_dir = os.path.dirname(resolved)
        if not os.path.isdir(parent_dir):
            try:
                os.makedirs(parent_dir, exist_ok=True)
            except OSError as e:
                return f"错误：无法创建父目录「{parent_dir}」: {e}"
        if not os.access(parent_dir, os.W_OK):
            return f"错误：目录不可写「{parent_dir}」"

        if len(content) > _MAX_WRITE_CHARS:
            content = content[:_MAX_WRITE_CHARS]

        try:
            with open(resolved, "a", encoding="utf-8") as f:
                f.write(content)
        except PermissionError:
            return f"错误：无权写入文件「{resolved}」"
        except OSError as e:
            return f"错误：追加写入时出错「{resolved}」: {e}"

        size_bytes = len(content.encode("utf-8"))
        size_str = _format_file_size(size_bytes)
        line_count = content.count("\n") + 1 if content else 0
        return (
            f"✅ 已追加到文件末尾: {resolved}\n"
            f"  写入: {size_str}\n"
            f"  行数: {line_count}\n"
            f"  字符数: {len(content)}"
        )

    resolved = _resolve_write_path(path, force)
    if resolved is None:
        real_path = os.path.realpath(path)
        parent_dir = os.path.dirname(real_path)
        if not os.path.isdir(parent_dir):
            try:
                os.makedirs(parent_dir, exist_ok=True)
            except OSError as e:
                return f"错误：无法创建父目录「{parent_dir}」: {e}"
            resolved = real_path
        else:
            if os.path.exists(real_path):
                return f"错误：文件已存在「{path}」。如需覆盖请设置 force=True。"
            return f"错误：无法写入文件「{path}」（路径无效，请检查路径是否正确）"

    parent = os.path.dirname(resolved)
    if not os.access(parent, os.W_OK):
        return f"错误：目录不可写「{parent}」"

    if len(content) > _MAX_WRITE_CHARS:
        content = content[:_MAX_WRITE_CHARS]

    try:
        with open(resolved, "w", encoding="utf-8") as f:
            f.write(content)
    except PermissionError:
        return f"错误：无权写入文件「{resolved}」"
    except OSError as e:
        return f"错误：写入文件时出错「{resolved}」: {e}"

    # 注册到读取状态，使后续 edit_file 可直接使用
    try:
        _READ_FILE_STATE[resolved] = {
            "content": content,
            "mtime": os.path.getmtime(resolved),
            "full_read": True,
            "encoding": "utf-8",
            "line_ending": "CRLF" if "\r\n" in content else "LF",
        }
    except OSError:
        pass

    size_bytes = len(content.encode("utf-8"))
    size_str = _format_file_size(size_bytes)
    line_count = content.count("\n") + 1 if content else 0
    return (
        f"✅ 文件已写入: {resolved}\n"
        f"  大小: {size_str}\n"
        f"  行数: {line_count}\n"
        f"  字符数: {len(content)}"
    )


TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "list_directory",
            "description": "列出目录内容。返回文件/目录/链接列表，含大小和修改时间。支持排序和隐藏文件控制。仅接受绝对路径。不适用于读取文件内容、搜索文件内容或获取单个文件的详细信息（应使用 read_file、grep_search 或 file_info）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "目录的绝对路径"},
                    "include_hidden": {"type": "boolean", "description": "是否包含隐藏文件（以 . 开头的文件）", "default": False},
                    "sort_by": {"type": "string", "description": "排序方式", "enum": ["name", "size", "time"], "default": "name"},
                    "reverse": {"type": "boolean", "description": "是否反向排序", "default": False}
                },
                "required": ["path"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "读取文本文件的内容。支持指定行范围（从 start_line 到 end_line），以及控制最大字符数。仅接受绝对路径。读取后可通过 edit_file 工具编辑。不适用于列出目录内容、搜索文件内容或编辑文件。返回文件原始文本内容。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "文件的绝对路径"},
                    "purpose": {"type": "string", "description": "简短描述操作该文件的目的（10-40字），用于向用户解释执行此操作的原因。例如：检查配置文件、查看日志错误、阅读源代码"},
                    "max_chars": {"type": "integer", "description": "最大返回字符数（500-200000，默认 20000）", "default": 20000},
                    "start_line": {"type": "integer", "description": "起始行号（从 1 开始，默认 1）", "default": 1},
                    "end_line": {"type": "integer", "description": "结束行号（含，可选）"}
                },
                "required": ["path"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "创建新文件或覆盖现有文件的内容。仅接受绝对路径。默认不覆盖已有文件（需设置 force=True）。支持 append 追加模式。不适用于编辑已有文件中的部分内容（应使用 edit_file），不适用于重命名或删除文件。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "文件的绝对路径"},
                    "purpose": {"type": "string", "description": "简短描述写入该文件的目的（10-40字），用于向用户解释执行此操作的原因。例如：创建配置文件、写入测试数据"},
                    "content": {"type": "string", "description": "写入的文本内容"},
                    "force": {"type": "boolean", "description": "是否覆盖已存在的文件", "default": False},
                    "mode": {"type": "string", "description": "写入模式", "enum": ["write", "append"], "default": "write"}
                },
                "required": ["path", "content"]
            }
        }
    },
        {
            "type": "function",
            "function": {
                "name": "edit_file",
                "description": "编辑现有文件：替换字符串（string 模式，根据 old_string 匹配定位）或替换行范围（line 模式）。string 模式任意读取过文件即可编辑（old_string 匹配确保安全），line 模式需完整读取（无字符串兜底）。设置 force=True 跳过所有读取检查。支持 replace_all 替换全部匹配。不适用于创建新文件（应使用 write_file），不适用于重命名或删除文件。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "文件的绝对路径"},
                        "purpose": {"type": "string", "description": "简短描述编辑该文件的目的（10-40字），用于向用户解释执行此操作的原因。例如：修复语法错误、更新配置项、替换旧版权信息"},
                        "old_string": {"type": "string", "description": "要替换的原文（string 模式下必填）"},
                        "new_string": {"type": "string", "description": "替换后的新内容"},
                        "replace_all": {"type": "boolean", "description": "是否替换所有出现位置", "default": False},
                        "mode": {"type": "string", "description": "编辑模式", "enum": ["string", "line"], "default": "string"},
                        "start_line": {"type": "integer", "description": "替换的起始行号（line 模式）"},
                        "end_line": {"type": "integer", "description": "替换的结束行号（line 模式，含）"},
                        "force": {"type": "boolean", "description": "跳过前置读取检查，强制编辑", "default": False}
                    },
                    "required": ["path"]
                }
            }
        },
    {
        "type": "function",
        "function": {
            "name": "file_info",
            "description": "获取文件或目录的详细信息：类型、大小、权限、所有者、修改/访问/创建时间。支持符号链接。仅接受绝对路径。不适用于列出目录内容或读取文件内容（应使用 list_directory 或 read_file）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "文件或目录的绝对路径"}
                },
                "required": ["path"]
            }
        }
    },
]
