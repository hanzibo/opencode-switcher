"""
ai_text_utils — AI text processing utilities package.

Previously a single 1047-line file, now split into 5 domain modules:
  - image.py:    Image processing and vision model detection
  - math.py:     LaTeX expression escaping and fixing
  - cleanup.py:  Text cleaning, title extraction, avatar HTML
  - markdown.py: Markdown-to-HTML rendering pipeline
  - render.py:   Three-zone turn rendering (reasoning/tool/answer)

All public symbols are re-exported here for backward compatibility.
New code should import from the specific submodule (e.g.,
``from ai_text_utils.markdown import _markdown_to_html_safe``).
"""

# ── image ──
from .image import (
    _image_to_data_uri,
    _image_hash_path,
    _cached_image_to_data_uri,
    _vision_content_to_text,
    _vision_content_to_markdown,
    _resolve_vision_image_src,
    _model_supports_vision,
)

# ── math ──
from .math import (
    _LATEX_COMMANDS,
    _escape_math,
    _unescape_math,
    _fix_latex,
)

# ── cleanup ──
from .cleanup import (
    USER_AVATAR_HTML,
    ASSISTANT_AVATAR_HTML,
    _DIV_CLOSE_LEN,
    _dict_to_chat_message,
    _extract_after_header,
    _strip_ai_markup,
    _clean_history_title,
    _extract_local_title,
    _preserve_newlines,
    _close_unclosed_code_blocks,
)

# ── markdown ──
from .markdown import (
    _get_markdown_lib,
    _markdown_to_html_safe,
    _ensure_list_blankline,
    _ensure_table_blankline,
    _fix_blockquote_fences,
    _fix_details_blocks,
    _escape_tool_results,
    _unescape_tool_results,
    set_code_highlight,
)

# ── render ──
from .render import (
    _TOOL_DISPLAY_FIELD,
    _render_tool_step,
    _render_reasoning_html,
    _render_tool_steps_html,
    _render_answer_html,
    _render_active_turn_to_html,
    _rebuild_markdown_from_messages,
)
