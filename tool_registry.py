"""
Tool Registry — Function Calling 工具定义与执行器

为 AI 面板提供 OpenAI-compatible 工具注册、调度和执行能力。
首期工具：web_search（网络搜索）

所有工具使用零新外部依赖（requests 和 stdlib only）。
"""

import datetime
import fnmatch
import json
import os
import pathlib
import stat
import subprocess
import urllib.parse
import re
import html
from html.parser import HTMLParser
from typing import Any, Dict, Final, List, Optional, Callable, Tuple

import requests


# ── Tool Definitions (OpenAI function calling schema) ──────────────────────

TOOL_DEFINITIONS: List[Dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "搜索互联网获取实时信息。当用户问及实时新闻、最新动态、技术文档、或不确认的知识时使用。返回搜索结果列表（标题、URL、摘要）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "搜索关键词，使用中文或英文关键词均可"
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "返回结果数量（1-10，默认 5）",
                        "default": 5
                    }
                },
                "required": ["query"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "web_fetch",
            "description": "获取指定 URL 的页面内容并转换为纯文本。用于阅读文章、文档、新闻等具体页面。",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "要获取的页面完整 URL（以 http:// 或 https:// 开头）"
                    },
                    "max_chars": {
                        "type": "integer",
                        "description": "最多返回字符数（默认 5000）",
                        "default": 5000
                    }
                },
                "required": ["url"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "list_directory",
            "description": "列出指定目录的内容。仅接受绝对路径。返回文件和子目录列表，每条显示类型标记（[DIR] 目录、[FILE] 文件、[LINK] 符号链接）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "要列出的目录的绝对路径"
                    },
                    "include_hidden": {
                        "type": "boolean",
                        "description": "是否包含隐藏文件（以 . 开头），默认不包含",
                        "default": False
                    }
                },
                "required": ["path"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "读取指定文件的内容。仅接受绝对路径，自动检测并拒绝二进制文件。返回文件的纯文本内容。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "要读取的文件的绝对路径"
                    },
                    "max_chars": {
                        "type": "integer",
                        "description": "最多返回字符数（默认 5000，最大 50000）",
                        "default": 5000
                    }
                },
                "required": ["path"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_current_time",
            "description": "获取当前日期和时间。支持可选时区参数。默认返回系统本地时间。用于回答当前时间、日期、星期几等问题。",
            "parameters": {
                "type": "object",
                "properties": {
                    "timezone": {
                        "type": "string",
                        "description": "时区名称，如「Asia/Shanghai」「America/New_York」「UTC」。留空则使用系统本地时区。",
                        "default": ""
                    }
                },
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "grep_search",
            "description": "在目录中按正则表达式或关键词全文搜索文件内容。支持按文件通配模式过滤（如 *.py、*.{ts,js}）。自动跳过隐藏目录和二进制文件。仅接受绝对路径。",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "搜索的正则表达式或关键词（支持 Python re 语法）"
                    },
                    "path": {
                        "type": "string",
                        "description": "要搜索的根目录的绝对路径"
                    },
                    "include": {
                        "type": "string",
                        "description": "文件通配过滤模式，如「*.py」「*.{ts,js}」。留空则搜索所有文本文件。",
                        "default": ""
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "最多返回的匹配行数（1-200，默认 30）",
                        "default": 30
                    }
                },
                "required": ["pattern", "path"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "glob_find",
            "description": "按通配模式递归查找文件。仅接受绝对路径。自动跳过隐藏目录（以 . 开头）。返回匹配文件的绝对路径列表。",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "通配模式，如「**/*.py」「config*.json」「src/**/*.ts」"
                    },
                    "path": {
                        "type": "string",
                        "description": "搜索的根目录绝对路径"
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "最多返回的文件数（1-500，默认 100）",
                        "default": 100
                    }
                },
                "required": ["pattern", "path"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "file_info",
            "description": "获取文件或目录的元信息：大小、修改时间、访问时间、Unix 权限、类型（文件/目录/符号链接）、所有者等。仅接受绝对路径。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "文件或目录的绝对路径"
                    }
                },
                "required": ["path"]
            }
        }
    },
]

TOOL_CHOICE_AUTO = "auto"


# ── Utility: strip HTML tags ───────────────────────────────────────────────

class _HTMLTagStripper(HTMLParser):
    """Strip HTML tags, extracting clean text."""
    def __init__(self):
        super().__init__()
        self._text_parts: List[str] = []
        self._skip_tags = {"script", "style"}

    def handle_data(self, data):
        if not hasattr(self, "_in_skip") or not self._in_skip:
            stripped = data.strip()
            if stripped:
                self._text_parts.append(stripped)

    def handle_starttag(self, tag, attrs):
        if tag in self._skip_tags:
            self._in_skip = True

    def handle_endtag(self, tag):
        if tag in self._skip_tags:
            self._in_skip = False
        elif tag in ("p", "br", "div", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6"):
            self._text_parts.append("\n")

def strip_html(html_text: str) -> str:
    """Convert HTML to plain text."""
    stripper = _HTMLTagStripper()
    stripper.feed(html_text)
    return " ".join(stripper._text_parts)


# ── Web Search Parser ──────────────────────────────────────────────────────

class _DuckDuckGoResultParser(HTMLParser):
    """Parse DuckDuckGo HTML search results page."""
    def __init__(self, max_results: int):
        super().__init__()
        self.max_results = max_results
        self.results: List[Dict[str, str]] = []
        self._in_result_body = False
        self._in_title = False
        self._in_url = False
        self._in_snippet = False
        self._current: Dict[str, str] = {}
        self._div_depth = 0

    def handle_starttag(self, tag, attrs):
        attrs_dict = dict(attrs)
        cls = attrs_dict.get("class", "")

        if "result__body" in cls:
            self._in_result_body = True
            self._current = {}
            self._div_depth = 1
            return
        if not self._in_result_body:
            return

        if tag == "div":
            self._div_depth += 1

        if "result__title" in cls:
            self._in_title = True
        elif "result__url" in cls:
            self._in_url = True
        elif "result__snippet" in cls:
            self._in_snippet = True

    def handle_data(self, data):
        if self._in_title:
            self._current["title"] = self._current.get("title", "") + data
        elif self._in_url:
            self._current["url"] = self._current.get("url", "") + data
        elif self._in_snippet:
            self._current["snippet"] = self._current.get("snippet", "") + data

    def handle_endtag(self, tag):
        if self._in_result_body:
            if self._in_title and tag == "a":
                self._in_title = False
            elif self._in_url and tag == "a":
                self._in_url = False
            elif self._in_snippet and tag == "a":
                self._in_snippet = False

            if tag == "div":
                self._div_depth -= 1
                if self._div_depth <= 0 and self._current.get("title"):
                    if self.results is not None and len(self.results) < self.max_results:
                        # Deduplicate by title
                        title = self._current.get("title", "").strip()
                        url = self._current.get("url", "").strip()
                        snippet = self._current.get("snippet", "").strip()
                        if title and not any(r["title"] == title for r in self.results):
                            self.results.append({
                                "title": title,
                                "url": url,
                                "snippet": snippet,
                            })
                    self._in_result_body = False
                    self._current = {}


# ── Path Safety ────────────────────────────────────────────────────────────

def _resolve_safe_path(path: str) -> Optional[str]:
    """Resolve a path safely.

    Returns the resolved absolute path if it exists, None otherwise.
    Requires absolute paths to prevent directory traversal attacks.
    """
    if not path or not isinstance(path, str) or not os.path.isabs(path):
        return None
    resolved = os.path.realpath(path)
    if not os.path.exists(resolved):
        return None
    return resolved


_MAX_DIRECTORY_LISTING = 200


def execute_list_directory(path: str, include_hidden: bool = False) -> str:
    """List contents of a directory. Accepts absolute paths only."""
    resolved = _resolve_safe_path(path)
    if resolved is None:
        return f"错误：目录不存在或路径无效「{path}」"
    if not os.path.isdir(resolved):
        return f"错误：路径不是目录「{resolved}」"
    try:
        entries = sorted(os.listdir(resolved))
    except PermissionError:
        return f"错误：无权访问目录「{resolved}」"
    except OSError as e:
        return f"错误：访问目录时出错「{resolved}」: {e}"

    filtered = []
    for name in entries:
        if not include_hidden and name.startswith("."):
            continue
        full = os.path.join(resolved, name)
        try:
            if os.path.islink(full):
                marker = "[LINK]"
            elif os.path.isdir(full):
                marker = "[DIR]"
            else:
                marker = "[FILE]"
        except OSError:
            marker = "[?]"
        marker = marker.ljust(6)
        filtered.append(f"{marker}  {name}")

    if not filtered:
        return f"目录「{resolved}」为空。"

    total = len(filtered)
    if total > _MAX_DIRECTORY_LISTING:
        filtered = filtered[:_MAX_DIRECTORY_LISTING]
        filtered.append(f"\n...（已截断，仅显示前 {_MAX_DIRECTORY_LISTING} 项，共 {total} 项）")

    result = f"📁 目录列表: {resolved}\n\n" + "\n".join(filtered)
    return result


def execute_read_file(path: str, max_chars: int = 5000) -> str:
    """Read a text file's content. Accepts absolute paths only."""
    resolved = _resolve_safe_path(path)
    if resolved is None:
        return f"错误：文件不存在或路径无效「{path}」"
    if not os.path.isfile(resolved):
        return f"错误：路径不是文件「{resolved}」"

    max_chars = max(500, min(50000, max_chars))

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
            content = f.read(max_chars)
            if f.read(1):
                content += f"\n\n...（内容已截断）"
            return content
    except UnicodeDecodeError:
        return f"错误：文件「{resolved}」不是有效的 UTF-8 文本文件。"
    except PermissionError:
        return f"错误：无权读取文件「{resolved}」"
    except OSError as e:
        return f"错误：读取文件时出错「{resolved}」: {e}"


# ── Time Query ─────────────────────────────────────────────────────────────

_WEEKDAYS_CN: Final[Tuple[str, ...]] = ("星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日")


def execute_get_current_time(timezone: str = "") -> str:
    """Get current date, time, weekday and timezone info.

    Args:
        timezone: IANA timezone name (e.g. "Asia/Shanghai", "UTC").
                  Empty string means system local time.

    Returns:
        Formatted string with current time info.
    """
    if timezone:
        try:
            import zoneinfo
            tz = zoneinfo.ZoneInfo(timezone)
            now = datetime.datetime.now(tz)
        except (ImportError, ModuleNotFoundError):
            return f"错误：当前 Python 版本不支持 zoneinfo，无法使用时区参数「{timezone}」。请留空 timezone 使用本地时间。"
        except (KeyError, TypeError, OSError):
            return f"错误：无效的时区名称「{timezone}」"
    else:
        now = datetime.datetime.now().astimezone()

    tz_name = now.tzname() or "?"

    weekday = _WEEKDAYS_CN[now.weekday()]
    ts = int(now.timestamp())

    # Compute UTC offset
    offset = now.utcoffset()
    if offset is not None:
        total_minutes = int(offset.total_seconds() // 60)
        offset_hours = total_minutes // 60
        offset_minutes = abs(total_minutes) % 60
        offset_str = f"UTC{offset_hours:+d}" + (f":{offset_minutes:02d}" if offset_minutes else "")
    else:
        offset_str = "?"

    return (
        f"当前时间：{now.strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"星期：{weekday}\n"
        f"时区：{tz_name} ({offset_str})\n"
        f"时间戳：{ts}"
    )


# ── Grep Search ─────────────────────────────────────────────────────────────

_MAX_GREP_RESULTS = 200


def _glob_match(filename: str, pattern: str) -> bool:
    """Check if filename matches a glob pattern, handling {a,b} brace expansion."""
    if "{" in pattern and "}" in pattern:
        import re as _re
        m = _re.search(r"\{([^}]+)\}", pattern)
        if m:
            alts = m.group(1).split(",")
            prefix = pattern[:m.start()]
            suffix = pattern[m.end():]
            return any(fnmatch.fnmatch(filename, prefix + a + suffix) for a in alts)
    return fnmatch.fnmatch(filename, pattern)

_MAX_LINES_PER_FILE = 50


def execute_grep_search(pattern: str, path: str, include: str = "",
                        max_results: int = 30) -> str:
    """Search file contents by regex/keyword in a directory tree.

    Args:
        pattern: Regex or keyword to search for (Python re syntax).
        path: Absolute path of the root directory to search.
        include: Glob pattern to filter files (e.g. "*.py", "*.{ts,js}").
                 Empty string means all text files.
        max_results: Maximum number of matching lines to return (1-200).

    Returns:
        Formatted string with matches per file.
    """
    resolved = _resolve_safe_path(path)
    if resolved is None:
        return f"错误：目录不存在或路径无效「{path}」"
    if not os.path.isdir(resolved):
        return f"错误：路径不是目录「{resolved}」"

    max_results = max(1, min(_MAX_GREP_RESULTS, max_results))
    max_lines_per_file = _MAX_LINES_PER_FILE

    try:
        compiled = re.compile(pattern)
    except re.error as e:
        return f"错误：无效的正则表达式「{pattern}」: {e}"

    matches: List[str] = []
    total_matches = 0
    file_count = 0

    for root, dirs, files in os.walk(resolved, topdown=True):
        # Skip hidden directories
        dirs[:] = [d for d in dirs if not d.startswith(".")]

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
                    continue  # skip binary
                with open(fpath, "r", encoding="utf-8", errors="replace") as f:
                    for lineno, line in enumerate(f, 1):
                        if len(file_matches) >= max_lines_per_file:
                            file_matches.append(f"    ...（该文件匹配超过 {max_lines_per_file} 行，已截断）")
                            break
                        if compiled.search(line):
                            stripped = line.rstrip("\n\r")
                            if len(stripped) > 500:
                                stripped = stripped[:500] + "..."
                            file_matches.append(f"    L{lineno}: {stripped}")
            except (PermissionError, OSError):
                continue

            if file_matches:
                relpath = os.path.relpath(fpath, resolved)
                matches.append(f"📄 {relpath}")
                matches.extend(file_matches)
                total_matches += sum(1 for m in file_matches if not m.startswith("    ..."))
                file_count += 1

    if not matches:
        return f"在目录「{resolved}」中没有找到匹配「{pattern}」的内容。"

    result = f"🔍 搜索「{pattern}」在 {resolved}\n共 {file_count} 个文件，{total_matches} 行匹配\n\n" + "\n".join(matches)

    if total_matches >= max_results:
        result += f"\n\n...（已达到最大显示行数 {max_results}，可能还有更多匹配）"

    return result


# ── Glob Find ───────────────────────────────────────────────────────────────

_MAX_GLOB_RESULTS = 500


def execute_glob_find(pattern: str, path: str, max_results: int = 100) -> str:
    """Recursively find files matching a glob pattern.

    Args:
        pattern: Glob pattern such as "**/*.py", "config*.json".
        path: Absolute path of the root directory to search.
        max_results: Maximum number of files to return (1-500).

    Returns:
        Formatted string with matched file paths.
    """
    resolved = _resolve_safe_path(path)
    if resolved is None:
        return f"错误：目录不存在或路径无效「{path}」"
    if not os.path.isdir(resolved):
        return f"错误：路径不是目录「{resolved}」"

    max_results = max(1, min(_MAX_GLOB_RESULTS, max_results))

    root = pathlib.Path(resolved)

    try:
        matched = sorted(root.rglob(pattern))
    except (PermissionError, OSError) as e:
        return f"错误：搜索文件时出错「{resolved}」: {e}"

    filtered: List[str] = []
    for m in matched:
        if not m.is_file() and not m.is_symlink():
            continue
        rel = m.relative_to(root)
        parts = rel.parts
        if any(p.startswith(".") for p in parts):
            continue
        filtered.append(str(m.resolve()))

    if not filtered:
        return f"在目录「{resolved}」中没有找到匹配「{pattern}」的文件。"

    total = len(filtered)
    if total > max_results:
        filtered = filtered[:max_results]

    result = f"📂 搜索模式「{pattern}」在 {resolved}\n共 {total} 个匹配" + (f"（显示前 {max_results} 个）" if total > max_results else "") + "\n\n"
    result += "\n".join(filtered)

    if total > max_results:
        result += f"\n\n...（已截断，仅显示前 {max_results} 个，共 {total} 个）"

    return result


# ── File Info ───────────────────────────────────────────────────────────────

_FILE_SIZE_UNITS = ["B", "KB", "MB", "GB", "TB"]


def _format_file_size(size_bytes: int) -> str:
    if size_bytes == 0:
        return "0 B"
    import math
    unit_idx = int(math.floor(math.log(size_bytes, 1024)))
    unit_idx = min(unit_idx, len(_FILE_SIZE_UNITS) - 1)
    value = size_bytes / (1024 ** unit_idx)
    if unit_idx == 0:
        return f"{int(value)} B"
    return f"{value:.1f} {_FILE_SIZE_UNITS[unit_idx]}"


def execute_file_info(path: str) -> str:
    """Get file/directory metadata: size, mtime, atime, permissions, type, owner.

    Args:
        path: Absolute path of the file or directory.

    Returns:
        Formatted string with file metadata.
    """
    # Check symlink BEFORE path resolution
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
    perm_octal = oct(stat.S_IMODE(mode))[2:]  # e.g. "644", "755"
    perm_str = stat.filemode(mode)             # e.g. "-rw-r--r--"

    try:
        import pwd
        import grp
        owner = pwd.getpwuid(st.st_uid).pw_name
        group = grp.getgrgid(st.st_gid).gr_name
    except (ImportError, KeyError):
        owner = str(st.st_uid)
        group = str(st.st_gid)
    except Exception:
        owner = str(st.st_uid)
        group = str(st.st_gid)

    if os.path.isdir(resolved) and not is_symlink:
        size_str = "—（目录）"
    else:
        size_str = _format_file_size(st.st_size)

    mtime = datetime.datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M:%S")
    atime = datetime.datetime.fromtimestamp(st.st_atime).strftime("%Y-%m-%d %H:%M:%S")
    ctime = datetime.datetime.fromtimestamp(st.st_ctime).strftime("%Y-%m-%d %H:%M:%S")

    lines = [
        f"📋 文件信息: {resolved}",
        f"  类型: {file_type}",
        f"  大小: {size_str}",
        f"  权限: {perm_octal} ({perm_str})",
        f"  所有者: {owner}:{group}",
        f"  修改时间 (mtime): {mtime}",
        f"  访问时间 (atime): {atime}",
        f"  创建/状态变更 (ctime): {ctime}",
        f"  Inode: {st.st_ino}",
        f"  硬链接数: {st.st_nlink}",
    ]

    return "\n".join(lines)


# ── Tool Functions ─────────────────────────────────────────────────────────

# Max characters in a single tool result (to prevent token overflow)
MAX_TOOL_RESULT_CHARS = 5000

# Obscura headless browser binary (pre-installed)
_OBSCURA_BIN = os.environ.get("OBSCURA_BIN") or os.path.expanduser("~/.local/bin/obscura")

_OBSCURA_EVAL_TPL = """JSON.stringify(
    Array.from(document.querySelectorAll('.result__body')).slice(0, %d).map(el => ({
        title: el.querySelector('.result__title')?.textContent?.trim() || '',
        url: el.querySelector('.result__url')?.textContent?.trim() || '',
        snippet: el.querySelector('.result__snippet')?.textContent?.trim() || ''
    }))
)"""


def _format_search_results(results: List[Dict[str, str]], query: str) -> str:
    if not results:
        return f"没有找到关于「{query}」的搜索结果。"
    parts = []
    for i, r in enumerate(results, 1):
        title = r.get("title", "").strip()
        url_str = r.get("url", "").strip()
        snippet = r.get("snippet", "").strip()
        parts.append(f"{i}. {title}")
        if url_str:
            parts.append(f"   URL: {url_str}")
        if snippet:
            parts.append(f"   {snippet}")
        parts.append("")
    result_text = "\n".join(parts).strip()
    if len(result_text) > MAX_TOOL_RESULT_CHARS:
        result_text = result_text[:MAX_TOOL_RESULT_CHARS] + "\n\n...（结果已截断）"
    return result_text


def _execute_obscura_search(query: str, max_results: int) -> Optional[str]:
    """Execute search via Obscura headless browser + DuckDuckGo HTML endpoint.
    Returns None if Obscura is unavailable or fails."""
    if not os.path.isfile(_OBSCURA_BIN):
        return None
    encoded = urllib.parse.quote(query)
    url = f"https://html.duckduckgo.com/html/?q={encoded}"
    js = _OBSCURA_EVAL_TPL % max_results
    try:
        result = subprocess.run(
            [_OBSCURA_BIN, "fetch", url, "--quiet", "--eval", js, "--timeout", "20"],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            return None
        output = result.stdout.strip()
        if not output:
            return None
        parsed = json.loads(output)
        return _format_search_results(parsed, query)
    except (subprocess.TimeoutExpired, subprocess.CalledProcessError,
            json.JSONDecodeError, FileNotFoundError):
        return None


def _is_duckduckgo_captcha(html_text: str) -> bool:
    return "anomaly-modal" in html_text or "challenge-form" in html_text


def _execute_duckduckgo_search(query: str, max_results: int) -> str:
    """Execute search via DuckDuckGo HTML endpoint (requests, no JS)."""
    encoded = urllib.parse.quote(query)
    url = f"https://html.duckduckgo.com/html/?q={encoded}"
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/145.0.0.0 Safari/537.36"
        )
    }
    try:
        resp = requests.get(url, headers=headers, timeout=15)
        resp.raise_for_status()
    except requests.RequestException as e:
        return f"搜索失败：{e}"
    if _is_duckduckgo_captcha(resp.text):
        return (
            f"DuckDuckGo 搜索暂时被拦截（CAPTCHA 验证）。\n"
            f"当前为直连请求回退路径，搜索会被 CAPTCHA 拦截。"
        )
    parser = _DuckDuckGoResultParser(max_results)
    try:
        parser.feed(resp.text)
    except Exception:
        return f"解析搜索结果时出错"
    results = parser.results[:max_results]
    return _format_search_results(results, query)


def execute_web_search(query: str, max_results: int = 5) -> str:
    """Execute a web search. Uses Obscura (headless browser) as primary path,
    falls back to direct DuckDuckGo HTTP endpoint."""
    max_results = max(1, min(10, max_results))
    result = _execute_obscura_search(query, max_results)
    if result is not None:
        return result
    return _execute_duckduckgo_search(query, max_results)


_SUSPICIOUS_PATTERNS = re.compile(
    r"(Access Denied|Please enable JavaScript|请启用 JavaScript|"
    r"Your browser does not support JavaScript|Just a moment|"
    r"Checking your browser|DDoS protection|captcha|challenge)",
    re.IGNORECASE,
)


def _is_cjk(ch: str) -> bool:
    """Check if character is in CJK ideograph, punctuation or fullwidth range."""
    val = ord(ch)
    return (
        (0x4E00 <= val <= 0x9FFF) or      # CJK Unified Ideographs
        (0x3400 <= val <= 0x4DBF) or      # CJK Ext A
        (0x3000 <= val <= 0x303F) or      # CJK Symbols & Punctuation
        (0xFF00 <= val <= 0xFFEF) or      # Halfwidth/Fullwidth Forms
        (0x3040 <= val <= 0x309F) or      # Hiragana
        (0x30A0 <= val <= 0x30FF) or      # Katakana
        (0xAC00 <= val <= 0xD7AF)         # Hangul Syllables
    )


def _try_requests_fetch(url: str, max_chars: int) -> Optional[str]:
    """Try fetching a page via plain HTTP requests. Returns None on failure
    or if the result looks suspicious (too short, garbled, JS-required page)."""
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/145.0.0.0 Safari/537.36"
        )
    }
    try:
        resp = requests.get(url, headers=headers, timeout=20)
        resp.raise_for_status()
    except requests.RequestException:
        return None

    text = strip_html(resp.text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    text = text.strip()

    # Suspicious: too short (JS-rendered page got only shell HTML)
    if len(text) < 200:
        return None

    # Suspicious: contains known JS-required / captcha / error patterns
    if _SUSPICIOUS_PATTERNS.search(text):
        return None

    # Check for excessive garbled content: >15% non-ASCII (excluding CJK) characters
    if text:
        non_ascii_non_cjk = sum(1 for ch in text if ord(ch) > 127 and not _is_cjk(ch))
        if non_ascii_non_cjk / len(text) > 0.15:
            return None

    if len(text) > max_chars:
        text = text[:max_chars] + f"\n\n...（内容已截断）"
    return text



def _try_obscura_fetch(url: str, max_chars: int) -> str:
    """Fetch a page via Obscura headless browser with --dump markdown."""
    if not os.path.isfile(_OBSCURA_BIN):
        return f"获取页面失败：Obscura 不可用"
    try:
        result = subprocess.run(
            [_OBSCURA_BIN, "fetch", url, "--dump", "markdown", "--quiet", "--timeout", "20"],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            return f"获取页面失败：Obscura 返回错误码 {result.returncode}"
        text = result.stdout.strip()
        if not text:
            return f"获取页面失败：Obscura 返回空内容"
    except (subprocess.TimeoutExpired, subprocess.CalledProcessError,
            FileNotFoundError) as e:
        return f"获取页面失败：{e}"

    if len(text) > max_chars:
        text = text[:max_chars] + f"\n\n...（内容已截断）"
    return text


def execute_web_fetch(url: str, max_chars: int = 5000) -> str:
    """Fetch a page's content as plain text.

    Tries plain HTTP requests (fast, zero overhead) first. If the result
    is suspicious — too short (JS-rendered shell), contains JS-required /
    captcha patterns, or garbled non-ASCII — falls back to Obscura headless
    browser (--dump markdown) which executes JavaScript and renders the
    real page content.
    """
    max_chars = max(500, min(20000, max_chars))

    result = _try_requests_fetch(url, max_chars)
    if result is not None:
        return result

    return _try_obscura_fetch(url, max_chars)


# ── Tool Executor Registry ─────────────────────────────────────────────────

TOOL_EXECUTORS: Dict[str, Callable] = {
    "web_search": execute_web_search,
    "web_fetch": execute_web_fetch,
    "list_directory": execute_list_directory,
    "read_file": execute_read_file,
    "get_current_time": execute_get_current_time,
    "grep_search": execute_grep_search,
    "glob_find": execute_glob_find,
    "file_info": execute_file_info,
}


def execute_tool_call(tool_call: dict) -> str:
    """Execute a single tool call and return the result as a string.

    Args:
        tool_call: OpenAI-format tool_call dict with "id", "type", "function" keys.
                   tool_call["function"] has "name" and "arguments" (JSON string).

    Returns:
        String result to be sent back as tool role content.
    """
    name = tool_call.get("function", {}).get("name", "")
    arguments_raw = tool_call.get("function", {}).get("arguments", "{}")

    try:
        arguments = json.loads(arguments_raw)
    except json.JSONDecodeError:
        return f"工具调用参数解析失败：无效的 JSON ({arguments_raw[:200]})"

    executor = TOOL_EXECUTORS.get(name)
    if not executor:
        return f"错误：未知工具「{name}」"

    try:
        return executor(**arguments)
    except Exception as e:
        return f"执行工具「{name}」时出错：{e}"


def format_tool_calls_for_display(tool_calls: List[dict]) -> str:
    """Format tool calls into an HTML snippet for WebView display.

    Returns a string with HTML like:
        <div class="tool-call-info">🔍 <b>网络搜索：</b>查询内容</div>
    """
    if not tool_calls:
        return ""

    parts = []
    for tc in tool_calls:
        name = tc.get("function", {}).get("name", "")
        try:
            args = json.loads(tc.get("function", {}).get("arguments", "{}"))
        except (json.JSONDecodeError, TypeError):
            args = {}

        if name == "web_search":
            query = args.get("query", "")
            safe_query = html.escape(query)
            parts.append(f'<div class="tool-call-info">🔍 <b>网络搜索：</b>{safe_query}</div>')
        elif name == "web_fetch":
            url = args.get("url", "")
            safe_url = html.escape(url)
            parts.append(f'<div class="tool-call-info">📄 <b>获取页面：</b>{safe_url}</div>')
        elif name == "list_directory":
            path = args.get("path", "")
            safe_path = html.escape(path)
            parts.append(f'<div class="tool-call-info">📁 <b>列出目录：</b>{safe_path}</div>')
        elif name == "read_file":
            path = args.get("path", "")
            safe_path = html.escape(path)
            parts.append(f'<div class="tool-call-info">📝 <b>读取文件：</b>{safe_path}</div>')
        elif name == "get_current_time":
            tz = args.get("timezone", "")
            if tz:
                safe_tz = html.escape(tz)
                parts.append(f'<div class="tool-call-info">🕐 <b>查询时间：</b>{safe_tz}</div>')
            else:
                parts.append(f'<div class="tool-call-info">🕐 <b>查询时间：</b>本地时间</div>')
        elif name == "grep_search":
            pattern = args.get("pattern", "")
            search_path = args.get("path", "")
            safe_pattern = html.escape(pattern)
            safe_path = html.escape(search_path)
            parts.append(f'<div class="tool-call-info">🔍 <b>搜索内容：</b>{safe_pattern} 在 {safe_path}</div>')
        elif name == "glob_find":
            gpattern = args.get("pattern", "")
            gpath = args.get("path", "")
            safe_gpattern = html.escape(gpattern)
            safe_gpath = html.escape(gpath)
            parts.append(f'<div class="tool-call-info">📂 <b>查找文件：</b>{safe_gpattern} 在 {safe_gpath}</div>')
        elif name == "file_info":
            fpath = args.get("path", "")
            safe_fpath = html.escape(fpath)
            parts.append(f'<div class="tool-call-info">📋 <b>文件信息：</b>{safe_fpath}</div>')
        else:
            safe_name = html.escape(name)
            parts.append(f'<div class="tool-call-info">🔧 <b>工具调用：</b>{safe_name}</div>')

    return "\n".join(parts)


def render_collapsible_tool_result(name: str, content: str) -> str:
    """Render tool result block into collapsible HTML structure."""
    safe_name = html.escape(name)
    MAX_TOOL_DISPLAY = 2000
    display = content[:MAX_TOOL_DISPLAY]
    if len(content) > MAX_TOOL_DISPLAY:
        display += f"\n\n...（结果已截断，共 {len(content)} 字符）"
    safe_display = html.escape(display)

    return (
        f'<div class="tool-result-box">'
        f'<div class="tool-result-header">'
        f'<span>📎 工具结果 ({safe_name})</span>'
        f'<span class="tool-result-toggle" onclick="toggleToolResult(this)">展开</span>'
        f'</div>'
        f'<div class="tool-result-content" style="display: none;">\n'
        f'{safe_display}\n'
        f'</div>'
        f'</div>'
    )


def format_tool_result_for_display(name: str, content: str) -> str:
    """Format a tool execution result into an HTML snippet for WebView display."""
    return render_collapsible_tool_result(name, content)
