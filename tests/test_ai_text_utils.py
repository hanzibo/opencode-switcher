"""Unit tests for ai_text_utils — markdown, latex, and text cleaning utilities."""

import unittest

from ai_text_utils.markdown import (
    _markdown_to_html_safe,
    _ensure_table_blankline,
    _fix_latex,
    _escape_math,
    _unescape_math,
)
from ai_text_utils.render import _rebuild_markdown_from_messages
from ai_text_utils.cleanup import (
    _close_unclosed_code_blocks,
    _strip_ai_markup,
    _preserve_newlines,
)


class TestAITextUtils(unittest.TestCase):
    """Test text utility functions for markdown parsing and formatting."""

    def test_close_unclosed_code_blocks(self):
        unclosed = "Here is Python code:\n```python\ndef add(a, b):\n    return a + b"
        closed = _close_unclosed_code_blocks(unclosed)
        self.assertTrue(closed.endswith("\n```"))

    def test_fix_latex(self):
        raw_latex = r"\\\\frac{1}{2} + \\\\alpha"
        fixed = _fix_latex(raw_latex)
        self.assertIn(r"\frac{1}{2}", fixed)
        self.assertIn(r"\alpha", fixed)

    def test_escape_and_unescape_math(self):
        text = "Formula: $E = mc^2$ and block $$a^2 + b^2 = c^2$$"
        escaped, placeholders = _escape_math(text)
        self.assertNotIn("$E = mc^2$", escaped)
        restored = _unescape_math(escaped, placeholders)
        self.assertEqual(restored, text)

    def test_markdown_to_html_safe(self):
        md = "# Heading\n\nThis is **bold** text and `code`."
        html = _markdown_to_html_safe(md)
        self.assertIn("<h1>Heading</h1>", html)
        self.assertIn("<strong>bold</strong>", html)
        self.assertIn("<code>code</code>", html)

    # ── 表格边界（_ensure_table_blankline）──

    def test_table_blankline_before_table(self):
        """表格前无空行（既有逻辑回归）：表格正常渲染。"""
        md = "说明文字。\n| 名称 | 数量 |\n|---|---|\n| 苹果 | 3 |"
        html = _markdown_to_html_safe(md)
        self.assertIn("<table>", html)
        self.assertIn("<th>名称</th>", html)
        self.assertNotIn("说明文字。", html.split("<table>")[1])  # 说明文字不在表格内

    def test_table_blankline_after_table_text(self):
        """表格后紧跟普通文本：文本独立成段，不被并入表格。"""
        md = "| 名称 | 数量 |\n|---|---|\n| 苹果 | 3 |\n总结文字"
        html = _markdown_to_html_safe(md)
        self.assertIn("<table>", html)
        self.assertIn("<p>总结文字</p>", html)
        self.assertNotIn("<td>总结文字</td>", html)

    def test_table_blankline_after_table_quote(self):
        """表格后紧跟引用：引用独立成块，不被并入表格。"""
        md = "| A | B |\n|---|---|\n| 1 | 2 |\n> 引用文字"
        html = _markdown_to_html_safe(md)
        self.assertIn("<table>", html)
        self.assertIn("<blockquote>", html)
        self.assertNotIn("<td>&gt; 引用文字</td>", html)

    def test_table_blankline_adjacent_tables(self):
        """两个相邻表格（无文本间隔）：各自独立渲染，不合并。"""
        md = "| A | B |\n|---|---|\n| 1 | 2 |\n| C | D |\n|---|---|\n| 3 | 4 |"
        html = _markdown_to_html_safe(md)
        self.assertEqual(html.count("<table>"), 2, "相邻表格应渲染为两个独立表格")
        self.assertIn("<th>A</th>", html)
        self.assertIn("<th>C</th>", html)

    def test_table_blankline_inside_code_block(self):
        """代码块内的 | 行不受影响（不插入空行、不破坏代码）。"""
        md = "```\n| a | b |\n| 1 | 2 |\n```"
        html = _markdown_to_html_safe(md)
        self.assertNotIn("<table>", html)
        self.assertIn("| a | b |", html)

    def test_table_blankline_code_block_end_fence(self):
        """代码块内 | 行 + 结束 fence：不得在块内插入空行（Issue 1 回归）。

        旧版（无 ``` 排除逻辑）会在代码块结束 fence 前插入多余空行，
        此测试锁定转换结果与输入完全一致。
        """
        md = "```\n| a | b |\n| 1 | 2 |\n```"
        self.assertEqual(_ensure_table_blankline(md), md)
        html = _markdown_to_html_safe(md)
        self.assertNotIn("<table>", html)

    def test_table_blankline_adjacent_tables_no_trailing_pipe(self):
        """相邻表格（第二表分隔行无尾管线）：仍识别为两个独立表格（H1 回归）。"""
        md = "| A | B |\n|---|---|\n| 1 | 2 |\n| C | D |\n|---|---\n| 3 | 4 |"
        html = _markdown_to_html_safe(md)
        self.assertEqual(html.count("<table>"), 2, "无尾管线分隔行的相邻表格应渲染为两个独立表格")
        self.assertIn("<th>A</th>", html)
        self.assertIn("<th>C</th>", html)

    def test_strip_ai_markup(self):
        text_with_markup = 'Hello world <details class="thinking-details">internal reasoning</details>\n<div class="answer-header">Header</div>'
        cleaned = _strip_ai_markup(text_with_markup)
        self.assertNotIn("internal reasoning", cleaned)
        self.assertNotIn("answer-header", cleaned)
        self.assertIn("Hello world", cleaned)

    def test_preserve_newlines(self):
        text = "Line 1\nLine 2\n\n```python\nprint(1)\nprint(2)\n```"
        preserved = _preserve_newlines(text)
        self.assertIn("<br>", preserved)
        # Inside code block <br> should NOT be added
        self.assertNotIn("print(1)<br>", preserved)

    # ── 超长 bubble-region 保护（回归 5db9398） ──
    # Python-Markdown 对超长 HTML 块（reasoning > ~500 字符）解析失败，导致
    # 该块之前的所有内容丢失。P4 占位符保护 + 倒序恢复锁定此机制。

    def test_markdown_to_html_safe_protects_long_bubble_region(self):
        """超长 reasoning bubble-region：前置内容不丢失、无占位符残留。"""
        long_html = (
            '<div class="msg-row user" markdown="1">\n'
            '<div class="msg-bubble user" markdown="1">前置消息</div>\n'
            '</div>\n\n'
            '<div class="bubble-region reasoning-region">\n'
            '<div class="reasoning-badge complete"><span>Thought</span></div>\n'
            '<div class="reasoning-content" style="display:none;">'
            + ("x" * 3000) +
            '</div>\n'
            '</div>\n'
            '<!-- /bubble-region -->\n'
        )
        html = _markdown_to_html_safe(long_html)
        self.assertIn('class="msg-row user"', html)          # 前置内容不丢失
        self.assertIn("reasoning-content", html)             # 长区域内容保留
        self.assertNotIn("TOOL_RESULT_PLACEHOLDER", html)    # 无占位符残留

    def test_rebuild_markdown_preserves_all_turns_with_long_reasoning(self):
        """完整消息重建含超长 reasoning：所有 user 轮次保留（原始 bug 场景）。"""
        messages = []
        for i in range(5):
            messages.append({"role": "user", "content": f"提问 {i}"})
            messages.append({
                "role": "assistant",
                "content": f"回答 {i}" if i % 2 == 0 else "",
                "reasoning_content": f"思考 {i} " + ("长" * 800 if i == 2 else "短" * 20),
            })
        md = _rebuild_markdown_from_messages(messages, show_details=True)
        final = _markdown_to_html_safe(md)
        self.assertEqual(final.count('class="msg-row user"'), 5)
        self.assertNotIn("TOOL_RESULT_PLACEHOLDER", final)


if __name__ == "__main__":
    unittest.main()
