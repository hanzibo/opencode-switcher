"""HTML 模板 shell 缓存测试：同键复用、主题/pygments 键区分、内容替换与防串扰。

无需显示环境或 WebKit 运行时 —— ``ai_html_template`` 仅在导入时读取
本地 CSS/JS 资源文件，``get_html_template()`` 本身是纯字符串操作。
"""
import unittest

from ai_engine.ai_html_template import (
    _get_html_shell,
    _HTML_SHELL_CACHE_MAX,
    _INITIAL_HTML_MARKER,
    get_html_template,
)


class TestHtmlShellCache(unittest.TestCase):
    """``_get_html_shell`` 按 (theme_name, pygments_css) 键缓存静态外壳。"""

    def setUp(self):
        _get_html_shell.cache_clear()

    def tearDown(self):
        _get_html_shell.cache_clear()

    def test_same_key_reuse_returns_same_object(self):
        s1 = _get_html_shell("dark", "")
        s2 = _get_html_shell("dark", "")
        self.assertIs(s1, s2)
        info = _get_html_shell.cache_info()
        self.assertEqual(info.hits, 1)
        self.assertEqual(info.currsize, 1)

    def test_distinct_theme_keys_produce_distinct_shells(self):
        dark = _get_html_shell("dark", "")
        light = _get_html_shell("light", "")
        self.assertIsNot(dark, light)
        self.assertIn('body class="dark"', dark)
        self.assertIn('body class="light"', light)

    def test_theme_css_vars_are_substituted_per_theme(self):
        dark = _get_html_shell("dark", "")
        light = _get_html_shell("light", "")
        # web_bg: dark = #0a0b10, light = #ffffff (theme_config.py LIGHT/DARK)
        self.assertIn("#0a0b10", dark)
        self.assertIn("#ffffff", light)
        # 主题变量不应残留未替换的占位符
        self.assertNotRegex(dark + light, r"\{bg_color\}")

    def test_distinct_pygments_keys_produce_distinct_shells(self):
        plain = _get_html_shell("dark", "")
        with_pyg = _get_html_shell("dark", ".codehilite{color:red}")
        other_pyg = _get_html_shell("dark", ".codehilite{color:blue}")
        self.assertIsNot(plain, with_pyg)
        self.assertIsNot(with_pyg, other_pyg)
        self.assertIn(".codehilite{color:red}", with_pyg)
        self.assertIn(".codehilite{color:blue}", other_pyg)
        # chat.css 自带基础 .codehilite 规则，故用整段样式判定差异
        self.assertNotIn(".codehilite{color:red}", plain)
        self.assertNotIn(".codehilite{color:blue}", plain)

    def test_shell_contains_marker_exactly_once(self):
        shell = _get_html_shell("dark", "")
        self.assertEqual(shell.count(_INITIAL_HTML_MARKER), 1)
        # marker 位于 #content 首部（其后为 show-older-bar 静态区块）
        self.assertIn(f'<div id="content">{_INITIAL_HTML_MARKER}', shell)

    def test_cache_is_bounded(self):
        for i in range(_HTML_SHELL_CACHE_MAX + 10):
            _get_html_shell("dark", f"pyg{i}")
        info = _get_html_shell.cache_info()
        self.assertLessEqual(info.currsize, _HTML_SHELL_CACHE_MAX)


class TestGetHtmlTemplateContent(unittest.TestCase):
    """``get_html_template`` 只替换内容槽，不引入串扰。"""

    def setUp(self):
        _get_html_shell.cache_clear()

    def tearDown(self):
        _get_html_shell.cache_clear()

    def test_initial_html_inserted_at_content_slot(self):
        html = get_html_template("dark", "<p>hello</p>", "")
        self.assertIn(f'<div id="content"><p>hello</p>', html)

    def test_empty_initial_html_gives_empty_content(self):
        html = get_html_template("dark", "", "")
        self.assertIn('<div id="content">', html)
        self.assertNotIn(_INITIAL_HTML_MARKER, html)
        self.assertIn('show-older-bar', html)  # 静态区块仍在 content 内

    def test_no_marker_leaks_into_output(self):
        html = get_html_template("dark", "content", "")
        self.assertNotIn(_INITIAL_HTML_MARKER, html)

    def test_same_key_different_content_does_not_leak(self):
        h1 = get_html_template("dark", "<p>AAA</p>", "")
        h2 = get_html_template("dark", "<p>BBB</p>", "")
        h3 = get_html_template("dark", "", "")
        self.assertIn("<p>AAA</p>", h1)
        self.assertIn("<p>BBB</p>", h2)
        self.assertNotIn("<p>BBB</p>", h1)
        self.assertNotIn("<p>AAA</p>", h2)
        self.assertNotIn("<p>AAA</p>", h3)
        self.assertNotIn("<p>BBB</p>", h3)

    def test_output_equals_shell_with_content_substituted(self):
        shell = _get_html_shell("dark", ".x{}")
        html = get_html_template("dark", "<b>hi</b>", ".x{}")
        self.assertEqual(
            html, shell.replace(_INITIAL_HTML_MARKER, "<b>hi</b>")
        )

    def test_output_contains_static_assets(self):
        html = get_html_template("dark", "", "")
        self.assertIn("<!DOCTYPE html>", html)
        self.assertIn("<style>", html)
        self.assertIn("_renderMath", html)
        self.assertIn("addCopyButtons", html)

    def test_initial_table_wrap_guard_is_embedded(self):
        """初始表格包裹（DOMContentLoaded + _wrapTables）必须嵌入模板。

        回归防线：load_html 嵌入 initial_html 的宽表格依赖该监听器包裹成
        横向滚动；若模板重构移除此逻辑，表格会退回固定宽度+强制换行。
        """
        html = get_html_template("dark", "", "")
        self.assertIn("DOMContentLoaded", html)
        self.assertIn("_wrapTables", html)
        # 判空保护也应存在（防止 #content 缺失时退化为全文档扫描）
        self.assertIn("if (content) _wrapTables(content)", html)

    def test_theme_and_pygments_variant_matches_key(self):
        html = get_html_template("light", "x", "PREFIX{color:red}")
        self.assertIn('body class="light"', html)
        self.assertIn("PREFIX{color:red}", html)

    def test_finish_reasoning_resets_reasoning_cache(self):
        """finishReasoning 必须清空 _reasoningCache，防止最新思考泄漏到历史轮次。"""
        html = get_html_template("dark", "", "")
        self.assertIn("_reasoningCache = '';", html)


if __name__ == "__main__":
    unittest.main()
