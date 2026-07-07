"""AI text processing utilities extracted from clipboard_panel.py.

Pure functions (zero GTK dependency) for markdown/math processing,
LLM message rendering, and text cleaning. Extracted to reduce the
7713-line monolith in clipboard_panel.py.
"""

import re
import html
import os
import json
import base64
import mimetypes
from typing import Optional, List, Dict, Tuple, Union

import tool_registry
from clipboard_store import ChatMessage, CONFIG_DIR

# Python markdown extensions used for AI panel rendering
_MARKDOWN_EXTENSIONS = [
    "fenced_code", "codehilite", "tables", "md_in_html", "def_list",
    "pymdownx.tilde",   # ~~删除线~~ 和 ~下标~
    "pymdownx.caret",   # ^上标^
    "pymdownx.mark",    # ==高亮==
]

USER_AVATAR_HTML = (
    '<div class="msg-avatar user">'
    '<svg viewBox="0 0 24 24" width="16" height="16" fill="currentColor">'
    '<path d="M12 12c2.21 0 4-1.79 4-4s-1.79-4-4-4-4 1.79-4 4 1.79 4 4 4zm0 2c-2.67 0-8 1.34-8 4v2h16v-2c0-2.66-5.33-4-8-4z"/>'
    '</svg>'
    '</div>'
)

ASSISTANT_AVATAR_HTML = (
    '<div class="msg-avatar assistant">'
    '<svg viewBox="0 0 24 24" width="18" height="18" fill="currentColor">'
    '<path d="M12 2L14.8 9.2L22 12L14.8 14.8L12 22L9.2 14.8L2 12L9.2 9.2L12 2Z"/>'
    '</svg>'
    '</div>'
)

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


def _strip_ai_markup(text: str) -> str:
    """Strip AI panel HTML markup (thinking blocks, headers, divs) from assistant content."""
    text = re.sub(
        r'<details class=["\']thinking-details["\'].*?</details>\n*',
        "", text, flags=re.DOTALL
    )
    text = re.sub(
        r'<div class=["\'](?:assistant|thinking|answer)-header["\'].*?</div>\n*',
        "", text, flags=re.DOTALL
    )
    text = re.sub(
        r'</?div.*?>',
        "", text
    )
    return text.strip()


def _escape_tool_results(text: str) -> Tuple[str, List[str]]:
    """Scan the text for tool result box HTML chunks and escape them using placeholders."""
    # Matches the outer tool-result-box up to the marker comment, anchored to start/end of line
    pattern = re.compile(r'(?:^|\n)(<div class="tool-result-box">.*?<!-- tool-result-marker -->)(?=\n|$)', re.DOTALL)
    placeholders = []

    def repl(match):
        placeholder = f"\n<!--TOOL_RESULT_PLACEHOLDER_{len(placeholders)}-->"
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
        html_text = p_pattern.sub(lambda m: original, html_text)

        html_text = html_text.replace(placeholder, original)
        html_text = html_text.replace(escaped_placeholder, original)
    return html_text


def _fix_blockquote_fences(text: str) -> str:
    """Convert blockquote sections containing code fences to raw HTML."""
    lines = text.split('\n')
    i = 0
    result = []
    in_code_block = False

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        if stripped.startswith('```'):
            in_code_block = not in_code_block

        if not in_code_block and line.startswith('>'):
            bq_lines = []
            while i < len(lines) and lines[i].startswith('>'):
                bq_lines.append(lines[i])
                i += 1

            stripped_bq = []
            for bl in bq_lines:
                if bl.startswith('> '):
                    stripped_bq.append(bl[2:])
                elif bl == '>':
                    stripped_bq.append('')
                else:
                    stripped_bq.append(bl[1:])

            # Only convert if blockquote contains a code fence
            has_fence = any(s.lstrip().startswith('```') for s in stripped_bq)

            if has_fence:
                inner = '\n'.join(stripped_bq)
                result.append(f'\n<blockquote markdown="1">\n{inner}\n</blockquote>\n')
            else:
                result.extend(bq_lines)
        else:
            result.append(line)
            i += 1

    return '\n'.join(result)


def _fix_details_blocks(text: str) -> str:
    """Preprocess <details> tags to ensure markdown="1" attribute and proper blank line spacing.

    Without markdown="1", md_in_html extension skips parsing nested markdown inside details blocks.
    Without blank lines, tables/lists inside details blocks are squished and not recognized.
    """
    lines = text.split('\n')
    result = []
    i = 0
    in_code_block = False

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        stripped_lower = stripped.lower()

        if stripped.startswith('```'):
            in_code_block = not in_code_block

        if not in_code_block:
            # 1. Inject markdown="1" into details open tags if not present
            if stripped_lower.startswith('<details') and 'markdown' not in stripped_lower:
                line = re.sub(
                    r'<details(?![^>]*\bmarkdown\b)([^>]*)>',
                    r'<details\1 markdown="1">',
                    line,
                    flags=re.IGNORECASE
                )

            # 2. Insert blank line after summary block to allow nested markdown parsing
            if '</summary>' in stripped_lower:
                result.append(line)
                if i + 1 < len(lines):
                    next_stripped = lines[i + 1].strip()
                    if next_stripped and not next_stripped.startswith('</'):
                        result.append('')
                i += 1
                continue

            # 3. Insert blank line before details close tag to properly close nested tables
            if stripped_lower == '</details>':
                if result and result[-1].strip() and not result[-1].strip().startswith('<'):
                    result.append('')
                result.append(line)
                i += 1
                continue

        result.append(line)
        i += 1

    return '\n'.join(result)


def _markdown_to_html_safe(text: str, fallback_content: Optional[str] = None) -> str:
    escaped_text, placeholders = _escape_math(text)
    placeholders = [_fix_latex(p) for p in placeholders]
    escaped_text, tool_placeholders = _escape_tool_results(escaped_text)
    escaped_text = _ensure_list_blankline(escaped_text)
    escaped_text = _ensure_table_blankline(escaped_text)
    escaped_text = _fix_blockquote_fences(escaped_text)
    escaped_text = _fix_details_blocks(escaped_text)
    try:
        import markdown
        try:
            import markdown.util
            block_elements = markdown.util.BLOCK_LEVEL_ELEMENTS
            if isinstance(block_elements, list):
                if 'details' not in block_elements:
                    block_elements.append('details')
                if 'summary' not in block_elements:
                    block_elements.append('summary')
            elif hasattr(block_elements, 'add'):
                block_elements.add('details')
                block_elements.add('summary')
        except Exception:
            pass
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


def _render_tool_step(tool_call: dict, tool_result_msg: Optional[dict]) -> str:
    func = tool_call.get("function", {})
    name = func.get("name", "unknown")
    arguments_str = func.get("arguments", "{}")
    
    # Try to parse arguments for prettier display
    try:
        args = json.loads(arguments_str)
        args_display = ", ".join(f"{k}={json.dumps(v, ensure_ascii=False)}" for k, v in args.items())
    except Exception:
        args_display = arguments_str

    tool_icons = {
        "bash": "🖥️",
        "web_search": "🌐",
        "web_fetch": "📖",
        "read_file": "📄",
        "write_file": "✍️",
        "edit_file": "✏️",
        "delete_file": "🗑️",
        "list_directory": "📁",
        "glob_find": "🔍",
        "grep_search": "🔎",
        "get_current_time": "⏰",
        "todo_create": "📋",
        "todo_update": "📋",
        "todo_list": "📋",
        "sub_agent": "🤖",
        "get_subagent_status": "🤖",
        "read_qq_mail": "📧"
    }
    icon = tool_icons.get(name, "⚙️")

    # Status and result
    status_icon = "✅"
    result_html = ""
    
    if tool_result_msg:
        content = tool_result_msg.get("content", "")
        if content.strip().startswith(tool_registry.ERROR_PREFIXES):
            status_icon = "❌"
        
        # Max limit for display to avoid slowing down Webview
        MAX_DISPLAY = 4000
        display_content = content[:MAX_DISPLAY]
        if len(content) > MAX_DISPLAY:
            display_content += f"\n\n...（结果已截断，共 {len(content)} 字符）"
            
        safe_content = html.escape(display_content)
        result_html = (
            f'<div class="tool-step-result">\n'
            f'<pre><code>{safe_content}</code></pre>\n'
            f'</div>\n'
        )
    else:
        status_icon = '<span class="tool-step-status running">🔄</span>'
        result_html = '<div class="tool-step-result"><em>正在运行中...</em></div>\n'

    return (
        f'<details class="tool-step-details">\n'
        f'<summary class="tool-step-summary">\n'
        f'<span class="tool-step-status">{status_icon}</span>\n'
        f'<strong>调用工具: {icon} {name}</strong>\n'
        f'</summary>\n'
        f'<div class="tool-step-content">\n'
        f'<div class="tool-step-args"><strong>参数:</strong> <code>{html.escape(args_display)}</code></div>\n'
        f'{result_html}'
        f'</div>\n'
        f'</details>\n'
    )


def _render_active_turn_to_html(
    turn_messages: List[Dict],
    streaming_reasoning: str = "",
    streaming_content: str = "",
    is_streaming: bool = False
) -> str:
    # 1. Gather all reasoning
    reasoning_parts = []
    for msg in turn_messages:
        if msg.get("role") == "assistant" and msg.get("reasoning_content"):
            reasoning_parts.append(msg["reasoning_content"])
    if streaming_reasoning:
        reasoning_parts.append(streaming_reasoning)
        
    reasoning_text = "\n".join(reasoning_parts).strip()
    reasoning_html = ""
    if reasoning_text:
        # If streaming, keep it open; otherwise collapse it
        open_attr = ' open' if is_streaming and streaming_reasoning else ''
        escaped = html.escape(reasoning_text)
        reasoning_html = (
            f'<details class="thinking-details"{open_attr}>\n'
            f'<summary class="thinking-summary">💭 Thinking Process</summary>\n'
            f'<div class="thinking-content">{escaped}</div>\n'
            f'</details>\n\n'
        )

    # 2. Pair tool calls and results
    tool_results_by_id = {}
    legacy_tool_results = []
    for msg in turn_messages:
        if msg.get("role") == "tool":
            cid = msg.get("tool_call_id")
            if cid:
                tool_results_by_id[cid] = msg
            else:
                legacy_tool_results.append(msg)

    tool_calls_list = []
    for msg in turn_messages:
        if msg.get("role") == "assistant" and msg.get("tool_calls"):
            for tc in msg["tool_calls"]:
                tool_calls_list.append(tc)

    tool_steps_html = ""
    if tool_calls_list:
        steps_list = []
        for i, tc in enumerate(tool_calls_list):
            cid = tc.get("id")
            result_msg = None
            if cid and cid in tool_results_by_id:
                result_msg = tool_results_by_id[cid]
            elif i < len(legacy_tool_results):
                result_msg = legacy_tool_results[i]
                
            steps_list.append(_render_tool_step(tc, result_msg))
            
        tool_steps_html = (
            f'<div class="tool-steps-container">\n'
            f'{"".join(steps_list)}'
            f'</div>\n\n'
        )

    # 3. Gather final answer content
    content_parts = []
    for msg in turn_messages:
        if msg.get("role") == "assistant" and msg.get("content"):
            content_str = msg["content"]
            content_str = _strip_ai_markup(content_str)
            if content_str.strip():
                content_parts.append(content_str)
                
    if streaming_content:
        content_parts.append(streaming_content)

    final_content = "\n".join(content_parts).strip()
    content_html = ""
    if final_content:
        rendered_md = _markdown_to_html_safe(final_content)
        content_html = (
            f'<div class="answer-header">💡 Answer:</div>\n'
            f'{rendered_md}\n'
        )

    return f'{reasoning_html}{tool_steps_html}{content_html}'


def _rebuild_markdown_from_messages(messages: List[Dict]) -> str:
    """Convert OpenAI-format message list back to rendered markdown text."""
    if not messages:
        return ""
    parts = []
    i = 0
    while i < len(messages):
        m = messages[i]
        role = m.get("role", "")
        content = m.get("content", "")
        
        if role == "user":
            if isinstance(content, list):
                rendered_content = _vision_content_to_markdown(content)
            else:
                rendered_content = _close_unclosed_code_blocks(content)
            parts.append(
                f'<div class="msg-row user" markdown="1">\n'
                f'{USER_AVATAR_HTML}\n'
                f'<div class="msg-bubble user" markdown="1">\n'
                f'{rendered_content}\n'
                f'<copy-marker data-msg-index="{i}" class="user-copy-marker"></copy-marker>\n'
                f'</div>\n'
                f'</div>\n\n'
            )
            i += 1
            continue
            
        elif role == "assistant" or role == "tool":
            # Start of assistant response turn.
            # Gather all assistant and tool messages in this turn.
            turn_msgs = []
            start_idx = i
            while i < len(messages) and messages[i].get("role") in ("assistant", "tool"):
                turn_msgs.append(messages[i])
                i += 1
            
            turn_html = _render_active_turn_to_html(turn_msgs, is_streaming=False)
            if turn_html.strip():
                parts.append(
                    f'<div class="msg-row assistant" markdown="1">\n'
                    f'{ASSISTANT_AVATAR_HTML}\n'
                    f'<div class="msg-bubble assistant" markdown="1">\n'
                    f'{turn_html}\n'
                    f'<copy-marker data-msg-index="{start_idx}"></copy-marker>\n'
                    f'</div>\n'
                    f'</div>\n\n'
                )
            continue
            
        i += 1
    return "".join(parts)
