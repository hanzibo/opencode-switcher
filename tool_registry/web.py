"""Web search and fetch tools — search the web and fetch page content."""

import json
import os
import re
import subprocess
import threading
import time
import urllib.parse
from html.parser import HTMLParser
from typing import Any, Dict, List, Optional, Tuple

import requests


# Max characters in a single tool result (to prevent token overflow)
MAX_TOOL_RESULT_CHARS = 20000

# Returned when user cancels during tool execution
_TOOL_CANCELLED = "工具调用已被用户取消"


def _run_subprocess_cancellable(args, cancel_event, timeout, **kwargs):
    """Run subprocess with cancel_event support.
    Returns (stdout_text, False) on success, or (None, True) if cancelled."""
    deadline = time.monotonic() + timeout
    process = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, **kwargs)
    try:
        while time.monotonic() < deadline:
            if cancel_event and cancel_event.is_set():
                process.kill()
                return None, True
            try:
                stdout, _stderr = process.communicate(timeout=0.5)
                return stdout, False
            except subprocess.TimeoutExpired:
                continue
        process.kill()
        raise subprocess.TimeoutExpired(args, timeout)
    finally:
        if process.returncode is None:
            process.kill()
            process.wait()

# Obscura headless browser binary (pre-installed)
_OBSCURA_BIN = os.environ.get("OBSCURA_BIN") or os.path.expanduser("~/.local/bin/obscura")

_OBSCURA_EVAL_TPL = """JSON.stringify(
    Array.from(document.querySelectorAll('.result__body')).slice(0, %d).map(el => ({
        title: el.querySelector('.result__title')?.textContent?.trim() || '',
        url: el.querySelector('.result_url')?.textContent?.trim() || '',
        snippet: el.querySelector('.result__snippet')?.textContent?.trim() || ''
    }))
)"""

# Web fetch response cache (5 min TTL, LRU eviction at 50 entries)
_FETCH_CACHE: Dict[str, Tuple[float, str]] = {}
_FETCH_CACHE_TTL = 300
_FETCH_CACHE_MAX = 50


def _get_cached_fetch(url: str) -> Optional[str]:
    entry = _FETCH_CACHE.get(url)
    if entry is not None and time.monotonic() - entry[0] < _FETCH_CACHE_TTL:
        return entry[1]
    _FETCH_CACHE.pop(url, None)
    return None


def _set_cached_fetch(url: str, content: str):
    while len(_FETCH_CACHE) >= _FETCH_CACHE_MAX:
        _FETCH_CACHE.pop(next(iter(_FETCH_CACHE)), None)
    _FETCH_CACHE[url] = (time.monotonic(), content)


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


def _execute_obscura_search(query: str, max_results: int,
                            cancel_event: Optional[threading.Event] = None) -> Optional[str]:
    if not os.path.isfile(_OBSCURA_BIN):
        return None
    encoded = urllib.parse.quote(query)
    url = f"https://html.duckduckgo.com/html/?q={encoded}"
    js = _OBSCURA_EVAL_TPL % max_results
    try:
        stdout, cancelled = _run_subprocess_cancellable(
            [_OBSCURA_BIN, "fetch", url, "--quiet", "--eval", js, "--timeout", "20"],
            cancel_event, timeout=30,
        )
        if cancelled:
            return _TOOL_CANCELLED
        if not stdout:
            return None
        output = stdout.strip()
        if not output:
            return None
        parsed = json.loads(output)
        return _format_search_results(parsed, query)
    except (subprocess.TimeoutExpired, subprocess.CalledProcessError,
            json.JSONDecodeError, FileNotFoundError):
        return None


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


def _is_duckduckgo_captcha(html_text: str) -> bool:
    return "anomaly-modal" in html_text or "challenge-form" in html_text


def _execute_duckduckgo_search(query: str, max_results: int,
                               cancel_event: Optional[threading.Event] = None) -> str:
    encoded = urllib.parse.quote(query)
    url = f"https://html.duckduckgo.com/html/?q={encoded}"
    headers = {
        "User-Agent": ("Mozilla/5.0 (X11; Linux x86_64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/145.0.0.0 Safari/537.36")
    }
    # Run HTTP request in a thread so cancel_event can be polled
    result_box = []
    exc_box = []
    def _request():
        try:
            result_box.append(requests.get(url, headers=headers, timeout=15))
        except requests.RequestException as e:
            exc_box.append(e)
    thread = threading.Thread(target=_request, daemon=True)
    thread.start()
    while thread.is_alive():
        if cancel_event and cancel_event.is_set():
            return _TOOL_CANCELLED
        thread.join(timeout=0.5)
    if exc_box:
        return f"搜索失败：{exc_box[0]}"
    resp = result_box[0]
    if _is_duckduckgo_captcha(resp.text):
        return ("DuckDuckGo 搜索暂时被拦截（CAPTCHA 验证）。\n"
                "当前为直连请求回退路径，搜索会被 CAPTCHA 拦截。")
    parser = _DuckDuckGoResultParser(max_results)
    try:
        parser.feed(resp.text)
    except Exception:
        return "解析搜索结果时出错"
    results = parser.results[:max_results]
    return _format_search_results(results, query)


def execute_web_search(query: str, max_results: int = 5,
                       cancel_event: Optional[threading.Event] = None) -> str:
    """Execute a web search. Uses direct DuckDuckGo HTTP as primary path (fast),
    falls back to Obscura headless browser if DuckDuckGo is blocked."""
    max_results = max(1, min(10, max_results))
    result = _execute_duckduckgo_search(query, max_results, cancel_event=cancel_event)
    if result == _TOOL_CANCELLED:
        return result
    if not result.startswith(("DuckDuckGo", "搜索失败")):
        return result
    result = _execute_obscura_search(query, max_results, cancel_event=cancel_event)
    if result == _TOOL_CANCELLED:
        return result
    if result is not None:
        return result
    return "搜索失败：所有搜索路径均不可用。"


_SUSPICIOUS_PATTERNS = re.compile(
    r"(Access Denied|Please enable JavaScript|请启用 JavaScript|"
    r"Your browser does not support JavaScript|Just a moment|"
    r"Checking your browser|DDoS protection|captcha|challenge)",
    re.IGNORECASE,
)


def _is_cjk(ch: str) -> bool:
    val = ord(ch)
    return (
        (0x4E00 <= val <= 0x9FFF) or
        (0x3400 <= val <= 0x4DBF) or
        (0x3000 <= val <= 0x303F) or
        (0xFF00 <= val <= 0xFFEF) or
        (0x3040 <= val <= 0x309F) or
        (0x30A0 <= val <= 0x30FF) or
        (0xAC00 <= val <= 0xD7AF)
    )


def _extract_with_trafilatura(html_text: str) -> Optional[str]:
    try:
        import trafilatura
    except ImportError:
        return None
    try:
        result = trafilatura.extract(
            html_text,
            output_format='markdown',
            include_comments=False,
            include_tables=True,
            no_fallback=False,
        )
        if result and result.strip():
            return result.strip()
    except Exception:
        pass
    return None


def _try_requests_fetch(url: str, max_chars: int, timeout: int = 20,
                        cancel_event: Optional[threading.Event] = None) -> Optional[str]:
    headers = {
        "User-Agent": ("Mozilla/5.0 (X11; Linux x86_64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/145.0.0.0 Safari/537.36")
    }
    result_box = []
    exc_box = []
    def _req():
        try:
            result_box.append(requests.get(url, headers=headers, timeout=timeout))
        except requests.RequestException as e:
            exc_box.append(e)
    thread = threading.Thread(target=_req, daemon=True)
    thread.start()
    while thread.is_alive():
        if cancel_event and cancel_event.is_set():
            return None
        thread.join(timeout=0.5)
    if exc_box:
        return None
    resp = result_box[0]

    trafilatura_text = _extract_with_trafilatura(resp.text)
    if trafilatura_text:
        text = trafilatura_text
    else:
        text = strip_html(resp.text)
        text = re.sub(r'\n{3,}', '\n\n', text)
        text = text.strip()

    if len(text) < 200:
        return None

    if _SUSPICIOUS_PATTERNS.search(text):
        return None

    if text:
        non_ascii_non_cjk = sum(1 for ch in text if ord(ch) > 127 and not _is_cjk(ch))
        if non_ascii_non_cjk / len(text) > 0.15:
            return None

    if len(text) > max_chars:
        text = text[:max_chars] + f"\n\n...（内容已截断）"
    return text


def _try_obscura_fetch(url: str, max_chars: int, timeout: int = 20,
                       cancel_event: Optional[threading.Event] = None) -> str:
    if not os.path.isfile(_OBSCURA_BIN):
        return "获取页面失败：Obscura 不可用"
    obs_timeout = max(10, timeout)
    try:
        stdout, cancelled = _run_subprocess_cancellable(
            [_OBSCURA_BIN, "fetch", url, "--dump", "markdown", "--quiet", "--timeout", str(obs_timeout)],
            cancel_event, timeout=obs_timeout + 10,
        )
        if cancelled:
            return _TOOL_CANCELLED
        text = stdout.strip() if stdout else ""
        if not text:
            return "获取页面失败：Obscura 返回空内容"
    except (subprocess.TimeoutExpired, subprocess.CalledProcessError,
            FileNotFoundError) as e:
        return f"获取页面失败：{e}"

    if len(text) > max_chars:
        text = text[:max_chars] + "\n\n...（内容已截断）"
    return text


def execute_web_fetch(url: str, max_chars: int = 5000, timeout: int = 20,
                      cancel_event: Optional[threading.Event] = None) -> str:
    """Fetch a page's content as plain text.
    Tries plain HTTP requests fast first; falls back to Obscura headless browser."""
    max_chars = max(500, min(20000, max_chars))
    timeout = max(5, min(60, timeout))

    cached = _get_cached_fetch(url)
    if cached is not None:
        return cached

    result = _try_requests_fetch(url, max_chars, timeout, cancel_event=cancel_event)
    if result is not None:
        _set_cached_fetch(url, result)
        return result
    if cancel_event and cancel_event.is_set():
        return _TOOL_CANCELLED

    result = _try_obscura_fetch(url, max_chars, timeout, cancel_event=cancel_event)
    _set_cached_fetch(url, result)
    return result


TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "搜索互联网获取实时信息。当用户问及实时新闻、最新动态、技术文档、或不确认的知识时使用。返回搜索结果列表（标题、URL、摘要）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "搜索关键词，使用中文或英文关键词均可"},
                    "max_results": {"type": "integer", "description": "返回结果数量（1-10，默认 5）", "default": 5}
                },
                "required": ["query"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "web_fetch",
            "description": "获取指定 URL 页面的内容。优先使用 HTTP 直连（轻量快速），页面含反爬或需 JS 渲染时自动回退到 Obscura 无头浏览器。结果缓存 5 分钟。",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "需要获取的网页 URL"},
                    "max_chars": {"type": "integer", "description": "最大返回字符数（500-20000，默认 5000）", "default": 5000},
                    "timeout": {"type": "integer", "description": "请求超时秒数（5-60，默认 20）", "default": 20}
                },
                "required": ["url"]
            }
        }
    },
]
