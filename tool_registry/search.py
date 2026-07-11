"""Search tools — grep text search and glob file search in directory trees."""

import datetime
import fnmatch
import os
import re
import stat
from typing import Dict, List, Optional

from .common import _get_ignore_dirs
from .filesystem import _format_file_size, _resolve_safe_path


_MAX_GREP_RESULTS = 500
_MAX_LINES_PER_FILE = 50
_MAX_GLOB_RESULTS = 500


def _glob_match(filename: str, pattern: str) -> bool:
    """Check if filename matches a glob pattern, handling {a,b} brace expansion."""
    if "{" in pattern and "}" in pattern:
        m = re.search(r"\{([^}]+)\}", pattern)
        if m:
            alts = m.group(1).split(",")
            prefix = pattern[:m.start()]
            suffix = pattern[m.end():]
            return any(fnmatch.fnmatch(filename, prefix + a + suffix) for a in alts)
    return fnmatch.fnmatch(filename, pattern)


def _grep_with_ripgrep(pattern: str, resolved: str, max_results: int,
                        include: str = "", ignore_case: bool = False,
                        literal: bool = False, context: int = 0,
                        max_chars: int = 8000,
                        format: str = "flat") -> str:
    import subprocess as _sp
    import json as _json

    max_lines_per_file = _MAX_LINES_PER_FILE

    cmd = ["rg", "--json", "--line-number", "--no-heading", "--color=never",
           "--hidden", "--max-columns", "500", "--max-count",
           str(max_lines_per_file)]
    if ignore_case:
        cmd.append("--ignore-case")
    if literal:
        cmd.append("--fixed-strings")
    if include:
        cmd.extend(["--glob", include])
    if context > 0:
        cmd.extend(["-C", str(min(context, 30))])
    cmd.extend(["--", pattern, str(resolved)])

    try:
        proc = _sp.run(cmd, capture_output=True, text=True, timeout=30)
    except _sp.TimeoutExpired:
        return f"错误：ripgrep 搜索超时（30s），请缩小搜索范围。"
    except OSError as e:
        return f"错误：ripgrep 执行失败: {e}"

    if proc.returncode not in (0, 1):
        stderr = proc.stderr.strip()
        if stderr:
            return f"错误：ripgrep 搜索出错: {stderr}"

    file_matches_map: Dict[str, List[str]] = {}
    file_total: Dict[str, int] = {}
    last_file = ""
    char_count = 0
    hit_char_limit = False

    for line in proc.stdout.splitlines():
        if not line.strip():
            continue
        try:
            event = _json.loads(line)
        except _json.JSONDecodeError:
            continue

        if event.get("type") == "begin":
            data = event.get("data", {})
            last_file = (data.get("path", {}).get("text", "") or "")
        elif event.get("type") == "match":
            data = event.get("data", {})
            fpath = data.get("path", {}).get("text", "") or last_file
            lineno = data.get("line_number", 0)
            text = data.get("lines", {}).get("text", "").rstrip("\n\r")
            if len(text) > 500:
                text = text[:500] + "..."
            if fpath not in file_matches_map:
                file_matches_map[fpath] = []
                file_total[fpath] = 0
            if file_total[fpath] < max_lines_per_file:
                line_str = f"    L{lineno}: {text}"
                char_count += len(line_str)
                if char_count > max_chars and not hit_char_limit:
                    hit_char_limit = True
                if not hit_char_limit or char_count <= max_chars:
                    file_matches_map[fpath].append(line_str)
                    file_total[fpath] += 1
                else:
                    if file_total[fpath] == 0 and not file_matches_map[fpath]:
                        file_matches_map[fpath].append(line_str)
                        file_total[fpath] += 1
            elif file_total[fpath] == max_lines_per_file:
                file_matches_map[fpath].append(
                    f"    ...（该文件匹配超过 {max_lines_per_file} 行，已截断）")
                file_total[fpath] += 1
        elif event.get("type") == "context":
            data = event.get("data", {})
            fpath = data.get("path", {}).get("text", "") or last_file
            lineno = data.get("line_number", 0)
            text = data.get("lines", {}).get("text", "").rstrip("\n\r")
            if fpath in file_matches_map:
                ctx_line = f"    -{lineno}- {text}"
                char_count += len(ctx_line)
                if char_count > max_chars and not hit_char_limit:
                    hit_char_limit = True
                if not hit_char_limit or char_count <= max_chars:
                    file_matches_map[fpath].append(ctx_line)

    if not file_matches_map:
        return f"在目录「{resolved}」中没有找到匹配「{pattern}」的内容。"

    all_files = sorted(file_matches_map.keys())
    total_matches = sum(file_total.values())
    lines_out: List[str] = []
    if format == "grouped":
        for fpath in all_files:
            relpath = os.path.relpath(fpath, resolved)
            cnt = file_total[fpath]
            lines_out.append(f"━━━ {relpath}（{cnt} 个匹配）━━━")
            lines_out.extend(file_matches_map[fpath])
            lines_out.append("")
    else:
        for fpath in all_files:
            relpath = os.path.relpath(fpath, resolved)
            lines_out.append(f"📄 {relpath}")
            lines_out.extend(file_matches_map[fpath])

    result = (f"🔍 搜索「{pattern}」在 {resolved}\n"
              f"共 {len(all_files)} 个文件，{total_matches} 行匹配\n\n" +
              "\n".join(lines_out))

    reasons = []
    if total_matches >= max_results:
        reasons.append(f"行数上限 {max_results}")
    if hit_char_limit:
        reasons.append(f"字符数上限 {max_chars}")
    if reasons:
        result += f"\n\n⚠️ 结果已截断（触达上限：{'，'.join(reasons)}），存在更多匹配"

    return result


def _grep_with_python(pattern: str, resolved: str, max_results: int,
                       include: str = "", ignore_case: bool = False,
                       literal: bool = False, context: int = 0,
                       max_chars: int = 8000,
                       format: str = "flat") -> str:
    max_lines_per_file = _MAX_LINES_PER_FILE

    try:
        if literal:
            compiled = re.compile(re.escape(pattern))
        elif ignore_case:
            compiled = re.compile(pattern, re.IGNORECASE)
        else:
            compiled = re.compile(pattern)
    except re.error as e:
        return f"错误：无效的正则表达式「{pattern}」: {e}"

    matches: List[str] = []
    file_matches_map: Dict[str, List[str]] = {}
    total_matches = 0
    file_count = 0
    char_count = 0
    hit_char_limit = False

    ignore_dirs = _get_ignore_dirs()
    for root, dirs, files in os.walk(resolved, topdown=True):
        dirs[:] = [d for d in dirs if not d.startswith(".") and d not in ignore_dirs]

        if total_matches >= max_results:
            break

        for fname in files:
            if total_matches >= max_results:
                break

            if fname.startswith("."):
                continue

            if include:
                if not _glob_match(fname, include):
                    continue

            fpath = os.path.join(root, fname)
            file_matches: List[str] = []

            try:
                with open(fpath, "rb") as f:
                    header = f.read(8192)
                if b"\x00" in header:
                    continue
                with open(fpath, "r", encoding="utf-8", errors="replace") as f:
                    all_lines = f.readlines()
            except (PermissionError, OSError):
                continue

            for idx, line in enumerate(all_lines):
                if len(file_matches) >= max_lines_per_file:
                    file_matches.append(
                        f"    ...（该文件匹配超过 {max_lines_per_file} 行，已截断）")
                    break
                if compiled.search(line):
                    stripped = line.rstrip("\n\r")
                    if len(stripped) > 500:
                        stripped = stripped[:500] + "..."
                    line_str = f"    L{idx + 1}: {stripped}"
                    char_count += len(line_str)
                    if char_count > max_chars and not hit_char_limit:
                        hit_char_limit = True
                    if not hit_char_limit or char_count <= max_chars:
                        file_matches.append(line_str)

            if file_matches:
                relpath = os.path.relpath(fpath, resolved)
                file_matches_map[relpath] = list(file_matches)
                matches.append(f"📄 {relpath}")
                matches.extend(file_matches)
                total_matches += sum(1 for m in file_matches
                                     if not m.startswith("    ..."))
                file_count += 1

        if hit_char_limit and char_count > max_chars:
            break

    if not matches:
        return f"在目录「{resolved}」中没有找到匹配「{pattern}」的内容。"

    if format == "grouped":
        grouped_lines: List[str] = []
        for relpath in sorted(file_matches_map.keys()):
            cnt = sum(1 for m in file_matches_map[relpath] if not m.startswith("    ..."))
            grouped_lines.append(f"━━━ {relpath}（{cnt} 个匹配）━━━")
            grouped_lines.extend(file_matches_map[relpath])
            grouped_lines.append("")
        result = (f"🔍 搜索「{pattern}」在 {resolved}\n"
                  f"共 {len(file_matches_map)} 个文件，{total_matches} 行匹配\n\n" +
                  "\n".join(grouped_lines))
    else:
        result = (f"🔍 搜索「{pattern}」在 {resolved}\n"
                  f"共 {file_count} 个文件，{total_matches} 行匹配\n\n" +
                  "\n".join(matches))

    reasons = []
    if total_matches >= max_results:
        reasons.append(f"行数上限 {max_results}")
    if hit_char_limit:
        reasons.append(f"字符数上限 {max_chars}")
    if reasons:
        result += f"\n\n⚠️ 结果已截断（触达上限：{'，'.join(reasons)}），存在更多匹配"

    return result


def execute_grep_search(pattern: str, path: str, include: str = "",
                        max_results: int = 50, ignore_case: bool = False,
                        literal: bool = False, context: int = 0,
                        max_chars: int = 8000,
                        format: str = "flat") -> str:
    """Search file contents by regex/keyword in a directory tree.
    Auto-detects ripgrep for fast search; falls back to pure-Python impl.
    """
    resolved = _resolve_safe_path(path)
    if resolved is None:
        return f"错误：目录不存在或路径无效「{path}」"
    if not os.path.isdir(resolved):
        return f"错误：路径不是目录「{resolved}」"

    max_results = max(1, min(_MAX_GREP_RESULTS, max_results))
    max_chars = max(500, min(200000, max_chars))

    if format not in ("flat", "grouped"):
        return "错误：format 必须是 'flat' 或 'grouped'。"

    import shutil as _shutil
    if _shutil.which("rg"):
        return _grep_with_ripgrep(
            pattern, resolved, max_results,
            include=include, ignore_case=ignore_case,
            literal=literal, context=context,
            max_chars=max_chars, format=format)
    return _grep_with_python(
        pattern, resolved, max_results,
        include=include, ignore_case=ignore_case,
        literal=literal, context=context,
        max_chars=max_chars, format=format)


def execute_glob_find(pattern: str, path: str, max_results: int = 100,
                      exclude: str = "") -> str:
    """Recursively find files matching a glob pattern, skipping blacklisted directories."""
    resolved = _resolve_safe_path(path)
    if resolved is None:
        return f"错误：目录不存在或路径无效「{path}」"
    if not os.path.isdir(resolved):
        return f"错误：路径不是目录「{resolved}」"

    max_results = max(1, min(_MAX_GLOB_RESULTS, max_results))

    entries: List[tuple] = []

    def _is_match(relpath: str, fname: str, pat: str) -> bool:
        if fnmatch.fnmatch(fname, pat):
            return True
        if fnmatch.fnmatch(relpath, pat):
            return True
        if pat.startswith("**/"):
            clean_pat = pat[3:]
            if fnmatch.fnmatch(relpath, clean_pat) or fnmatch.fnmatch(fname, clean_pat):
                return True
        return False

    try:
        ignore_dirs = _get_ignore_dirs()
        for root_dir, dirs, files in os.walk(resolved, topdown=True):
            dirs[:] = [d for d in dirs if not d.startswith(".") and d not in ignore_dirs]

            for fname in files:
                if fname.startswith("."):
                    continue
                if exclude and fnmatch.fnmatch(fname, exclude):
                    continue
                if exclude and fnmatch.fnmatch(os.path.join(root_dir, fname), exclude):
                    continue
                fpath = os.path.join(root_dir, fname)
                relpath = os.path.relpath(fpath, resolved)
                if _is_match(relpath, fname, pattern):
                    try:
                        st = os.lstat(fpath)
                        entries.append((st.st_mtime, relpath, fname, fpath, st))
                    except OSError:
                        entries.append((0, relpath, fname, fpath, None))
    except (PermissionError, OSError) as e:
        return f"错误：搜索文件时出错「{resolved}」: {e}"

    if not entries:
        return f"在目录「{resolved}」中没有找到匹配「{pattern}」的文件。"

    entries.sort(key=lambda e: e[0], reverse=True)

    total = len(entries)
    if total > max_results:
        entries = entries[:max_results]

    lines: List[str] = []
    for _, relpath, fname, fpath, st in entries:
        if st is not None:
            if stat.S_ISDIR(st.st_mode):
                marker = "DIR"
                size = "—"
            elif os.path.islink(fpath):
                marker = "LINK"
                size = _format_file_size(st.st_size)
            else:
                marker = "FILE"
                size = _format_file_size(st.st_size)
            mtime = datetime.datetime.fromtimestamp(st.st_mtime).strftime("%m-%d %H:%M")
        else:
            marker = "?"
            size = "?"
            mtime = "?"

        lines.append(f"[{marker:4s}] {size:>8s}  {mtime}  {relpath}")

    result = f"📂 搜索模式「{pattern}」在 {resolved}\n共 {total} 个匹配" + (f"（显示前 {len(entries)} 个）" if total > max_results else "") + "\n\n"
    result += "\n".join(lines)

    if total > max_results:
        result += f"\n\n...（已截断，仅显示前 {len(entries)} 个，共 {total} 个）"

    return result


TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "grep_search",
            "description": "在目录树中按正则表达式或关键词搜索文件内容。自动检测 ripgrep 加速，无 ripgrep 时使用 Python 实现。支持文件类型过滤。",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "搜索关键词或正则表达式"},
                    "path": {"type": "string", "description": "搜索根目录的绝对路径"},
                    "include": {"type": "string", "description": "文件类型过滤 glob，例如 *.py、*.{ts,js}"},
                    "max_results": {"type": "integer", "description": "最大返回行数（1-500，默认 50）", "default": 50},
                    "ignore_case": {"type": "boolean", "description": "是否忽略大小写", "default": False},
                    "literal": {"type": "boolean", "description": "是否将 pattern 视为普通字符串而非正则", "default": False},
                    "context": {"type": "integer", "description": "匹配行前后显示的上下文行数（0-30）", "default": 0},
                    "max_chars": {"type": "integer", "description": "结果最大字符数（500-200000，默认 8000）", "default": 8000},
                    "format": {"type": "string", "description": "输出格式：flat（默认，平铺列表）或 grouped（按文件分组）", "enum": ["flat", "grouped"], "default": "flat"}
                },
                "required": ["pattern", "path"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "glob_find",
            "description": "在目录树中递归搜索匹配 glob 模式的文件。支持 **/*.py、config*.json 等模式，返回文件路径、大小和修改时间。自动跳过 node_modules 等黑名单目录。",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "glob 搜索模式，例如 **/*.py、config*.json、src/**/*.ts"},
                    "path": {"type": "string", "description": "搜索根目录的绝对路径"},
                    "max_results": {"type": "integer", "description": "最大返回文件数（1-500，默认 100）", "default": 100},
                    "exclude": {"type": "string", "description": "排除的 glob 模式，例如 *__pycache__*"}
                },
                "required": ["pattern", "path"]
            }
        }
    },
]
