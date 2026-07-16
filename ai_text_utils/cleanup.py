"""Text cleaning and message conversion utilities.

Pure functions for stripping AI panel markup, cleaning conversation
titles, extracting local titles from messages, preserving newlines
in user input, closing unclosed code blocks, and converting message
dicts to ChatMessage dataclass.

Zero GTK dependency. Standalone module (no dependency on other
ai_text_utils submodules).
"""

import re
import html
from typing import Optional, List, Dict, Union

from clipboard_store import ChatMessage


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

_DIV_CLOSE_LEN = 6  # len('</div>')


def _dict_to_chat_message(m: dict) -> ChatMessage:
    """Convert an OpenAI-format message dict to a ChatMessage dataclass."""
    return ChatMessage(
        role=m.get("role", ""),
        content=m.get("content", ""),
        tool_call_id=m.get("tool_call_id"),
        name=m.get("name"),
        tool_calls=m.get("tool_calls"),
        reasoning_content=m.get("reasoning_content"),
    )


def _extract_after_header(raw: str, marker: str) -> Optional[str]:
    """Split on marker, return content after the header div's closing tag, or None if marker absent."""
    if marker not in raw:
        return None
    after = raw.split(marker, 1)[1]
    end = after.find('</div>')
    return after[end + _DIV_CLOSE_LEN:] if end != -1 else after


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
        from .image import _vision_content_to_text
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


def _close_unclosed_code_blocks(text: str) -> str:
    """Ensure that any unclosed markdown code blocks (triple backticks or tildes) are closed."""
    lines = text.splitlines()
    open_fence_char = None  # '`' or '~'
    open_fence_len = 0

    for line in lines:
        if open_fence_len == 0:
            # Look for opening fence (at most 3 leading spaces)
            leading_spaces = len(line) - len(line.lstrip(' '))
            if leading_spaces <= 3:
                match = re.match(r'^(`{3,}|~{3,})', line.lstrip(' '))
                if match:
                    fence = match.group(1)
                    open_fence_char = fence[0]
                    open_fence_len = len(fence)
        else:
            # Look for closing fence (at most 3 leading spaces, >= open_fence_len matching chars, no trailing non-space)
            leading_spaces = len(line) - len(line.lstrip(' '))
            if leading_spaces <= 3:
                pattern = r'^(' + re.escape(open_fence_char) + r'{' + str(open_fence_len) + r',})\s*$'
                if re.match(pattern, line.lstrip(' ')):
                    open_fence_len = 0
                    open_fence_char = None

    if open_fence_len > 0:
        closing_fence = open_fence_char * open_fence_len
        if text and not text.endswith('\n'):
            return text + '\n' + closing_fence
        return text + closing_fence

    return text


def _preserve_newlines(text: str) -> str:
    """Convert single newlines to <br> outside fenced code blocks.

    Standard Markdown collapses single newlines into spaces, making
    multi-line user input (Shift+Enter) render as a single paragraph.
    This inserts <br> for single newlines between non-empty lines outside
    code blocks, preserving the user's intended line breaks.
    """
    lines = text.split('\n')
    out = []
    in_code_block = False

    for line in lines:
        if line.strip().startswith('```'):
            in_code_block = not in_code_block
            out.append(line)
            continue

        if in_code_block:
            out.append(line)
            continue

        # Outside code block: add <br> before this line if previous line is non-empty
        # and the previous line itself is NOT a code fence (avoid breaking ```<br>)
        if out and out[-1].strip() and line.strip():
            prev_stripped = out[-1].strip()
            # 上一行是纯代码 fence（仅 ``` 或 ~~~ 组成）时，不加 <br>
            if re.match(r'^(`{3,}|~{3,})\s*$', prev_stripped):
                pass
            else:
                out[-1] += '<br>'

        out.append(line)

    return '\n'.join(out)



