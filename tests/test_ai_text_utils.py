"""Unit tests for ai_text_utils — markdown, latex, and text cleaning utilities."""

import unittest

from ai_text_utils.markdown import (
    _markdown_to_html_safe,
    _fix_latex,
    _escape_math,
    _unescape_math,
)
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


if __name__ == "__main__":
    unittest.main()
