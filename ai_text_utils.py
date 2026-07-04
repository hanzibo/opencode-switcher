"""AI text processing utilities extracted from clipboard_panel.py.

Pure functions (zero GTK dependency) for markdown/math processing,
LLM message rendering, and text cleaning. Extracted to reduce the
7713-line monolith in clipboard_panel.py.
"""

import re
import html
import os
import base64
import mimetypes
from typing import Optional, List, Dict, Tuple, Union

import tool_registry
from clipboard_store import ChatMessage, CONFIG_DIR

# Python markdown extensions used for AI panel rendering
_MARKDOWN_EXTENSIONS = ["fenced_code", "codehilite", "tables"]

# LaTeX commands that LLMs commonly double-escape (\\frac -> \frac, etc.)
_LATEX_COMMANDS = frozenset({
    "frac", "sqrt", "sum", "int", "prod", "lim", "sin", "cos", "log", "ln",
    "det", "begin", "end", "left", "right", "text", "mathrm", "mathbf",
    "mathit", "mathtt", "mathcal", "mathbb", "mathfrak", "displaystyle",
    "partial", "nabla", "infty", "alpha", "beta", "gamma", "delta", "epsilon",
    "theta", "lambda", "pi", "sigma", "omega", "varphi", "rightarrow", "leftarrow",
    "Rightarrow", "Leftarrow", "mapsto", "implies", "iff", "cdot", "times",
    "approx", "equiv", "neq", "leq", "geq", "subset", "supset", "cup", "cap",
})

_DIV_CLOSE_LEN = 6  # len('</div>')


def _image_to_data_uri(image_path: str) -> Optional[str]:
    """Read an image file and return a base64 data URI string.

    Detects mime type dynamically (fallback to image/png).
    """
    try:
        with open(image_path, "rb") as f:
            raw = f.read()
        b64 = base64.b64encode(raw).decode("utf-8")
        mime, _ = mimetypes.guess_type(image_path)
        if not mime or not mime.startswith("image/"):
            mime = "image/png"
        return f"data:{mime};base64,{b64}"
    except Exception:
        return None


def _image_hash_path(image_hash: str) -> str:
    """Return the absolute path to a cached image by its SHA-256 hash (16-char prefix)."""
    img_dir = os.path.join(CONFIG_DIR, "images")
    if os.path.isdir(img_dir):
        try:
            for f in os.listdir(img_dir):
                if f.startswith(image_hash):
                    return os.path.join(img_dir, f)
        except Exception:
            pass
    return os.path.join(img_dir, f"{image_hash}.png")


def _cached_image_to_data_uri(image_hash: str) -> Optional[str]:
    """Read a cached image by hash and return its data URI, or None if missing."""
    path = _image_hash_path(image_hash)
    if not os.path.isfile(path):
        return None
    return _image_to_data_uri(path)


def _vision_content_to_text(content: list) -> str:
    """Extract plain text from a vision content parts list, ignoring images."""
    texts = []
    for p in content:
        if isinstance(p, dict) and p.get("type") == "text":
            texts.append(p.get("text", ""))
    return "\n".join(texts)


def _vision_content_to_markdown(content: list) -> str:
    """Convert a vision content parts list to markdown+HTML representation.

    Text parts are joined and run through code-block closure.
    Image parts are resolved (hash->data URI) and rendered as ``<img>`` tags.
    """
    md_parts = []
    for p in content:
        if not isinstance(p, dict):
            continue
        if p.get("type") == "text":
            md_parts.append(p.get("text", ""))
        elif p.get("type") == "image_url":
            src = _resolve_vision_image_src([p])
            if src:
                md_parts.append(
                    f'<img src="{src}" class="chat-image" onclick="showLightbox(this.src)">'
                )
    md_text = "\n".join(md_parts)
    return _close_unclosed_code_blocks(md_text)


def _resolve_vision_image_src(content: list) -> Optional[str]:
    """Extract image source from vision content parts list.

    Handles both direct ``url`` (legacy) and ``hash`` reference formats.
    Returns a local ``file://`` URI or ``data:`` URI string, or None if no image part found/failed.
    """
    for p in content:
        if not isinstance(p, dict) or p.get("type") != "image_url":
            continue
        iu = p.get("image_url", {})
        url = iu.get("url", "")
        if url.startswith("data:") or url.startswith("file:"):
            return url
        h = iu.get("hash", "")
        if h:
            return "file://" + _image_hash_path(h)
    return None


def _model_supports_vision(model_name: str) -> bool:
    """启发式判断模型名称是否支持多模态视觉输入"""
    name_lower = model_name.lower()
    vision_keywords = [
        "vision", "vl", "gpt-4o", "claude-3", "gemini", "mimo", 
        "minicpm", "internvl", "llava", "qwen2.5-vl", "qwen-vl", "deepseek-vl"
    ]
    return any(kw in name_lower for kw in vision_keywords)


def _dict_to_chat_message(m: dict) -> ChatMessage:
    """Convert an OpenAI-format message dict to a ChatMessage dataclass."""
    return ChatMessage(
        role=m.get("role", ""),
        content=m.get("content", ""),
        tool_call_id=m.get("tool_call_id"),
        name=m.get("name"),
        tool_calls=m.get("tool_calls"),
    )


def _extract_after_header(raw: str, marker: str) -> Optional[str]:
    """Split on marker, return content after the header div's closing tag, or None if marker absent."""
    if marker not in raw:
        return None
    after = raw.split(marker, 1)[1]
    end = after.find('</div>')
    return after[end + _DIV_CLOSE_LEN:] if end != -1 else after


def _escape_math(text: str) -> Tuple[str, List[str]]:
    placeholders = []
    
    # 1. Protect block math: $$ ... $$ (multiline, not escaped)
    def replace_block(match):
        placeholder = f"<!--MATH_BLOCK_{len(placeholders)}-->"
        placeholders.append(match.group(0))
        return placeholder
    text = re.sub(r"(?<!\\)\$\$(.*?)(?<!\\)\$\$", replace_block, text, flags=re.DOTALL)
    
    # 2. Protect block math: \[ ... \] (multiline, not escaped)
    def replace_bracket(match):
        placeholder = f"<!--MATH_BLOCK_{len(placeholders)}-->"
        placeholders.append(match.group(0))
        return placeholder
    text = re.sub(r"(?<!\\)\\\[(.*?)(?<!\\)\\\]", replace_bracket, text, flags=re.DOTALL)
    
    # 3. Protect inline math: \( ... \) (must be before \begin{env} to avoid placeholder nesting)
    def replace_paren(match):
        placeholder = f"<!--MATH_INLINE_{len(placeholders)}-->"
        placeholders.append(match.group(0))
        return placeholder
    text = re.sub(r"(?<!\\)\\\((.*?)(?<!\\)\\\)", replace_paren, text)
    
    # 4. Protect inline math: $ ... $ (single line, not escaped, no space inside delimiters)
    def replace_inline(match):
        placeholder = f"<!--MATH_INLINE_{len(placeholders)}-->"
        placeholders.append(match.group(0))
        return placeholder
    text = re.sub(r"(?<!\\)\$(?!\s)([^$\n]+?)(?<!\s)(?<!\\)\$", replace_inline, text)
    
    # 5. Protect LaTeX environments: \begin{env} ... \end{env} (multiline, not escaped)
    def replace_env(match):
        placeholder = f"<!--MATH_BLOCK_{len(placeholders)}-->"
        placeholders.append(match.group(0))
        return placeholder
    text = re.sub(r"(?<!\\)\\begin\{([a-zA-Z*0-9]+)\}(.*?)\\end\{\1\}", replace_env, text, flags=re.DOTALL)

    return text, placeholders


def _unescape_math(html_text: str, placeholders: List[str]) -> str:
    for i, original in enumerate(placeholders):
        restored = html.escape(original)
        if original.strip().startswith("\\begin"):
            restored = f"$${restored}$$"
            
        html_text = html_text.replace(f"<!--MATH_BLOCK_{i}-->", restored)
        html_text = html_text.replace(f"<!--MATH_INLINE_{i}-->", restored)
        
        escaped_original = html.escape(original)
        html_text = html_text.replace(f"&lt;!--MATH_BLOCK_{i}--&gt;", escaped_original)
        html_text = html_text.replace(f"&lt;!--MATH_INLINE_{i}--&gt;", escaped_original)
        
    return html_text


def _escape_tool_results(text: str) -> Tuple[str, List[str]]:
    """Scan the text for tool result box HTML chunks and escape them using placeholders."""
    # Matches the outer tool-result-box up to the marker comment
    pattern = re.compile(r'(<div class="tool-result-box">.*?<!-- tool-result-marker -->)', re.DOTALL)
    placeholders = []

    def repl(match):
        placeholder = f"<!--TOOL_RESULT_PLACEHOLDER_{len(placeholders)}-->"
        placeholders.append(match.group(1))
        return placeholder

    escaped_text = pattern.sub(repl, text)
    return escaped_text, placeholders


def _unescape_tool_results(html_text: str, placeholders: List[str]) -> str:
    """Restore the escaped tool result box HTML chunks back to the final document."""
    for i, original in enumerate(placeholders):
        placeholder = f"<!--TOOL_RESULT_PLACEHOLDER_{i}-->"
        escaped_placeholder = f"&lt;!--TOOL_RESULT_PLACEHOLDER_{i}--&gt;"

        # Remove wrapping <p> tags generated by markdown for the placeholder comments
        p_pattern = re.compile(rf'<p>\s*({re.escape(placeholder)}|{re.escape(escaped_placeholder)})\s*</p>')
        html_text = p_pattern.sub(original, html_text)

        html_text = html_text.replace(placeholder, original)
        html_text = html_text.replace(escaped_placeholder, original)
    return html_text


def _markdown_to_html_safe(text: str, fallback_content: Optional[str] = None) -> str:
    escaped_text, placeholders = _escape_math(text)
    placeholders = [_fix_latex(p) for p in placeholders]
    escaped_text, tool_placeholders = _escape_tool_results(escaped_text)
    escaped_text = _ensure_list_blankline(escaped_text)
    escaped_text = _ensure_table_blankline(escaped_text)
    try:
        import markdown
        html_text = markdown.markdown(escaped_text, extensions=_MARKDOWN_EXTENSIONS)
    except ImportError:
        if fallback_content is not None:
            html_text = fallback_content
        else:
            html_text = f"<pre><code>{escaped_text}</code></pre>"
    html_text = _unescape_tool_results(html_text, tool_placeholders)
    return _unescape_math(html_text, placeholders)


def _ensure_list_blankline(text: str) -> str:
    """Ensure top-level lists are preceded by blank lines and normalize indent.

    Python markdown requires a blank line before <ul>/<ol> items.
    LLM output often omits these, causing list items to be rendered
    as plain text inside <p> tags (no line break before list).

    Also normalizes list item indentation to 4-space increments per
    nesting level. LLMs typically use 2 or 3 spaces per level, but
    Python markdown requires 4 spaces for proper nesting detection.

    Uses a stack of (orig_indent, norm_indent) tuples to track nesting
    levels: orig_indent is the original whitespace count from the source
    line, norm_indent is the normalized multiple-of-4 indent assigned
    to that level. Blank lines reset the stack.
    """
    lines = text.split('\n')
    result = []
    in_code_block = False
    list_stack = []
    for i, line in enumerate(lines):
        stripped = line.strip()

        if stripped.startswith('```'):
            in_code_block = not in_code_block

        if not in_code_block:
            is_list_item = bool(re.match(r'^[-*+]\s|^\d+[.)]\s', stripped))

            if is_list_item:
                orig_indent = len(line) - len(stripped)

                if i > 0 and not list_stack:
                    prev_line = lines[i - 1]
                    prev_stripped = prev_line.strip()
                    if prev_stripped:
                        result.append('')
                if not list_stack:
                    list_stack = [(orig_indent, 0)]
                    line = stripped
                elif orig_indent > list_stack[-1][0]:
                    new_indent = list_stack[-1][1] + 4
                    list_stack.append((orig_indent, new_indent))
                    line = ' ' * new_indent + stripped
                elif orig_indent == list_stack[-1][0]:
                    line = ' ' * list_stack[-1][1] + stripped
                else:
                    while list_stack and orig_indent < list_stack[-1][0]:
                        list_stack.pop()
                    if not list_stack:
                        list_stack = [(orig_indent, 0)]
                        line = stripped
                    elif orig_indent == list_stack[-1][0]:
                        line = ' ' * list_stack[-1][1] + stripped
                    else:
                        new_indent = list_stack[-1][1] + 4
                        list_stack.append((orig_indent, new_indent))
                        line = ' ' * new_indent + stripped
            elif not stripped:
                list_stack = []

        result.append(line)
    return '\n'.join(result)


def _ensure_table_blankline(text: str) -> str:
    """Ensure pipe tables are preceded by blank lines for Python-markdown's tables extension."""
    lines = text.split('\n')
    result = []
    in_code_block = False

    for i, line in enumerate(lines):
        stripped = line.strip()

        if stripped.startswith('```'):
            in_code_block = not in_code_block

        if not in_code_block and stripped.startswith('|'):
            if i > 0:
                prev_stripped = lines[i - 1].strip()
                if prev_stripped and not prev_stripped.startswith('|'):
                    result.append('')

        result.append(line)
    return '\n'.join(result)


def _close_unclosed_code_blocks(text: str) -> str:
    """Ensure that any unclosed markdown code blocks (triple backticks) are closed."""
    if text.count("```") % 2 != 0:
        return text + "\n```"
    return text


def _fix_latex(content: str) -> str:
    """Fix common LaTeX formatting errors produced by LLMs.

    Applied to captured math expressions BEFORE unescape so that
    restored content is already corrected. Covers:
      - double backslash before known commands (\\frac -> \frac)
      - missing \\end{env} for unclosed \\begin{env}
      - unclosed $$ (display math)
      - unclosed $ (inline math, per-line odd count heuristic)
    """
    known = sorted(_LATEX_COMMANDS, key=len, reverse=True)
    cmd_pattern = r'\\\\(?:' + '|'.join(known) + r')\b'
    content = re.sub(cmd_pattern, lambda m: m.group(0)[1:], content)

    envs = re.findall(r'(\\(?:begin|end))\{([a-zA-Z*0-9]+)\}', content)
    open_count = {}
    for cmd, name in envs:
        if cmd == '\\begin':
            open_count[name] = open_count.get(name, 0) + 1
        elif cmd == '\\end':
            open_count[name] = open_count.get(name, 0) - 1
    for name, count in open_count.items():
        if count > 0:
            content += f"\\end{{{name}}}" * count

    dollars = re.findall(r'(?<!\\)\$\$', content)
    if len(dollars) % 2 != 0:
        content += "$$"

    lines = content.split('\n')
    any_unclosed_inline = any(
        len(re.findall(r'(?<!\\)\$(?!\$)', line)) % 2 != 0
        for line in lines
    )
    if any_unclosed_inline:
        content += "$"

    return content


def _clean_history_title(title: str) -> str:
    """Clean up markdown markers and special prefixes from fallback titles for dropdown display."""
    if not title:
        return "(untitled)"
    
    # Strip HTML/XML tags
    title = re.sub(r"<[^>]+>", "", title)
    
    # Strip leading markdown structures (headers, list items, quotes, backticks)
    cleaned = title.strip()
    while True:
        prev = cleaned
        # Remove leading hashes, stars, dashes, plusses, angles, backticks, and whitespace
        cleaned = re.sub(r"^[\s#*>\-\+`]+", "", cleaned)
        # Strip surrounding quotes and standard markdown inline markers from the ends
        cleaned = cleaned.strip(" \"'()[]{}*`_~")
        if cleaned == prev:
            break
            
    # If the first line became empty, try to get the next non-empty line of the title (if multi-line)
    if not cleaned:
        for line in title.splitlines():
            line_cleaned = re.sub(r"^[\s#*>\-\+`]+", "", line.strip()).strip(" \"'()[]{}*`_~")
            if line_cleaned:
                cleaned = line_cleaned
                break
                
    if not cleaned:
        return "(untitled)"
    return cleaned


def _extract_local_title(message_content: Union[str, List, Dict]) -> str:
    """从消息文本中提取简短标题，作为 LLM 完成前的占位符。

    纯规则提取，零外部依赖，即时返回。LLM 生成完成后会替换此值。
    返回的标题一定是非空字符串（默认 fallback "New Conversation"）。
    """
    if isinstance(message_content, list):
        message_content = _vision_content_to_text(message_content)
    elif not isinstance(message_content, str):
        message_content = str(message_content) if message_content else ""
    if not message_content:
        return "New Conversation"
    first_line = message_content.split("\n")[0].strip()
    if not first_line:
        return "New Conversation"
    cleaned = re.sub(r"^[\s#*>\-+`]+", "", first_line).strip()
    cleaned = cleaned.strip(" \"'()[]{}*`_~")
    if not cleaned:
        return "New Conversation"
    if len(cleaned) > 25:
        cleaned = cleaned[:22] + "..."
    return cleaned


def _rebuild_markdown_from_messages(messages: List[Dict]) -> str:
    """Convert OpenAI-format message list back to rendered markdown text."""
    if not messages:
        return ""
    parts = []
    for i, m in enumerate(messages):
        role = m.get("role", "")
        content = m.get("content", "")
        tool_calls = m.get("tool_calls")

        if role == "tool":
            tool_name = m.get("name", "unknown")
            html_res = tool_registry.render_collapsible_tool_result(tool_name, content)
            parts.append(f"\n\n{html_res}\n\n")
            continue

        if role == "assistant" and tool_calls:
            # Show tool call info
            tc_html = tool_registry.format_tool_calls_for_display(tool_calls)
            if tc_html:
                parts.append("\n\n" + tc_html + "\n\n")
            # If there's also content, display it
            if isinstance(content, list):
                content = _vision_content_to_text(content)
            if content and content.strip():
                has_header = ('<div class="assistant-header">' in content
                             or '<div class="answer-header">' in content
                             or '<div class="thinking-header">' in content)
                prefix = '' if has_header else '\n\n<div class="assistant-header">🤖 Assistant:</div>\n\n'
                parts.append(f'{prefix}{content}\n\n')
            parts.append('\n\n---\n\n')
            continue

        if not content:
            continue

        if isinstance(content, list):
            rendered_content = _vision_content_to_markdown(content)
        else:
            rendered_content = _close_unclosed_code_blocks(content)

        if i == 0:
            parts.append(f'<div class="user-header">You:</div>\n\n{rendered_content}\n\n<copy-marker data-msg-index="{i}" class="user-copy-marker"></copy-marker>\n\n---\n\n')
        elif role == "user":
            parts.append(f'\n\n---\n\n<div class="user-header">You:</div>\n\n{rendered_content}\n\n<copy-marker data-msg-index="{i}" class="user-copy-marker"></copy-marker>\n\n---\n\n')
        elif role == "assistant":
            content_str = content if isinstance(content, str) else _vision_content_to_text(content)
            if content_str.strip():
                # Content already has role headers embedded from streaming phase;
                # avoid adding another .assistant-header wrapper to prevent duplication.
                has_header = ('<div class="assistant-header">' in content_str
                             or '<div class="answer-header">' in content_str
                             or '<div class="thinking-header">' in content_str)
                prefix = '' if has_header else '\n\n<div class="assistant-header">🤖 Assistant:</div>\n\n'
                parts.append(
                    f'{prefix}{content_str}\n\n'
                    f'<copy-marker data-msg-index="{i}"></copy-marker>\n\n---\n\n'
                )
    return "".join(parts)
