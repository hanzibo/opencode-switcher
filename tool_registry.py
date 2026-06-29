"""
Tool Registry — Function Calling 工具定义与执行器

为 AI 面板提供 OpenAI-compatible 工具注册、调度和执行能力。
首期工具：web_search（网络搜索）

所有工具使用零新外部依赖（requests 和 stdlib only）。
"""

import json
import os
import subprocess
import urllib.parse
import re
from html.parser import HTMLParser
from typing import Any, Dict, List, Optional, Callable

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
        self._depth = 0

    def handle_starttag(self, tag, attrs):
        attrs_dict = dict(attrs)
        cls = attrs_dict.get("class", "")

        if "result__body" in cls:
            self._in_result_body = True
            self._current = {}
            self._depth = 0
        if not self._in_result_body:
            return

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
            elif tag == "div":
                # Heuristic: close of nested div may close result__body
                # We track depth to handle nested divs properly
                pass

        if tag == "div" and self._in_result_body and self._current.get("title"):
            # Check if this closes the result__body by proximity
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


# ── Tool Functions ─────────────────────────────────────────────────────────

# Max characters in a single tool result (to prevent token overflow)
MAX_TOOL_RESULT_CHARS = 5000

# Obscura headless browser binary (pre-installed)
_OBSCURA_BIN = "/home/hzb/.local/bin/obscura"

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


def execute_web_fetch(url: str, max_chars: int = 5000) -> str:
    """Fetch a page's content as plain text."""
    max_chars = max(500, min(20000, max_chars))

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
    except requests.RequestException as e:
        return f"获取页面失败：{e}"

    text = strip_html(resp.text)
    # Collapse multiple whitespace
    text = re.sub(r'\n{3,}', '\n\n', text)
    text = text.strip()

    if len(text) > max_chars:
        text = text[:max_chars] + f"\n\n...（内容已截断）"

    return text


# ── Tool Executor Registry ─────────────────────────────────────────────────

TOOL_EXECUTORS: Dict[str, Callable] = {
    "web_search": execute_web_search,
    "web_fetch": execute_web_fetch,
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
            safe_query = _html_escape(query)
            parts.append(f'<div class="tool-call-info">🔍 <b>网络搜索：</b>{safe_query}</div>')
        elif name == "web_fetch":
            url = args.get("url", "")
            safe_url = _html_escape(url)
            parts.append(f'<div class="tool-call-info">📄 <b>获取页面：</b>{safe_url}</div>')
        else:
            safe_name = _html_escape(name)
            parts.append(f'<div class="tool-call-info">🔧 <b>工具调用：</b>{safe_name}</div>')

    return "\n".join(parts)


def format_tool_result_for_display(name: str, content: str) -> str:
    """Format a tool execution result into an HTML snippet for WebView display.

    Results are collapsed/hidden to avoid visual noise; tool calls are visible
    but results are only shown as a brief line.
    """
    safe_name = _html_escape(name)

    # Truncate very long results for display
    display = content
    if len(display) > 500:
        display = display[:500] + f"\n\n...（共 {len(content)} 字符）"

    safe_content = _html_escape(display)
    return (
        f'<div class="tool-result">'
        f'<b>📎 工具执行完成：</b>{safe_name}'
        f'</div>'
    )


def _html_escape(text: str) -> str:
    """Minimal HTML escaping."""
    return (text
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;"))
