"""Tests for the WebView-embedded AI header (紫月星云 spinner + 装饰区).

原独立微型 WebView（ai_engine/nebula_spinner.py）已删除——spinner 与 header
全部内联进主 WebView 模板（ai_engine/ai_html_template.py 的 #ai-header），
省掉第二个 WebKitWebProcess。这里验证模板结构与主题变量注入。
"""

import re
import unittest

from ai_engine.ai_html_template import _get_html_shell


class TestHeaderShell(unittest.TestCase):
    """模板静态结构：header 元素、spinner SVG、滚动布局。"""

    @classmethod
    def setUpClass(cls):
        # chat.css/js 编辑后需清 lru_cache，否则 shell 命中旧内容（test_html_template_cache.py 模式）
        _get_html_shell.cache_clear()
        cls.shell = _get_html_shell("dark-moon", "")

    def test_header_elements_present(self):
        for element in (
            'id="ai-header"',
            'id="ai-header-title"',
            'id="ai-header-model"',
            'id="ai-history-btn"',
            'id="ai-header-spinner"',
            'id="ai-close-btn"',
            'id="ai-history-dropdown"',
            'id="ai-history-list"',
            'id="ai-history-search"',
        ):
            self.assertIn(element, self.shell, f"模板缺少 {element}")

    def test_header_buttons_svg(self):
        # T1: header 按钮注入 inline SVG（显式 14px + aria-hidden，WebKit 固有尺寸陷阱规避）
        hist = re.search(r"<button id=\"ai-history-btn\".*?</button>", self.shell, re.S)
        close = re.search(r"<button id=\"ai-close-btn\".*?</button>", self.shell, re.S)
        self.assertTrue(hist and close, "按钮缺失")
        for m in (hist, close):
            self.assertIn("<svg", m.group(0))
            self.assertIn('width="14"', m.group(0))
            self.assertIn('aria-hidden="true"', m.group(0))

    def test_header_button_capsule(self):
        # T4: 胶囊按钮（scoped rule-block 断言）
        def block(sel):
            m = re.search(re.escape(sel) + r"\s*\{([^}]*)\}", self.shell)
            return m.group(1) if m else ""

        btn = block("#ai-header .ai-header-btn")
        self.assertIn("border-radius: 999px", btn)
        self.assertIn("inline-flex", btn)
        self.assertIn("transition", btn)
        self.assertIn(".ai-hdr-active", self.shell)
        self.assertIn(":active", self.shell)

    def test_close_btn_danger_color(self):
        # T4: 关闭按钮 hover 转警示色（{code_fg} 3 主题均红，零新增主题键）
        self.assertIn("#ai-close-btn:hover", self.shell)
        # {code_fg} 在 dark-moon 下替换为 #f472b6（theme_config.py:209）
        self.assertRegex(self.shell, r"#ai-close-btn:hover[^}]*#f472b6|#ai-close-btn:hover[^}]*\{code_fg\}")

    def test_nebula_svg_present(self):
        self.assertIn("crescentGrad", self.shell)
        self.assertIn("orbit-ring", self.shell)
        self.assertIn('class="dust', self.shell)
        self.assertIn("@keyframes nebula-spin", self.shell)

    def test_spinner_vars_injected(self):
        # dark-moon 紫月星云正式配色已注入（无残留占位符）
        self.assertIn("#e9d5ff", self.shell)                       # crescent_a
        self.assertIn("#7c3aed", self.shell)                       # crescent_b
        self.assertIn("rgba(192,132,252,0.55)", self.shell)        # orbit
        self.assertIn("#f0abfc", self.shell)                       # dust
        self.assertIn("#a855f7", self.shell)                       # glow
        for key in ("crescent_a", "crescent_b", "orbit", "dust", "glow"):
            self.assertNotIn("{" + key + "}", self.shell)

    def test_no_theme_placeholder_residue(self):
        # 全部 {key} 主题占位符必须被替换，无残留。
        # 注意：不能用对整段 shell 的 {[a-z_]+} 正则——KaTeX 自带 CSS/JS
        # 含 {array}/{c}/{lim} 等字面量，会误报。正确做法是逐 key 断言。
        from stores.theme_config import get_web_css_vars, get_ai_spinner_vars
        keys = set(dict(get_web_css_vars("dark-moon"))) | set(dict(get_ai_spinner_vars("dark-moon")))
        self.assertTrue(keys, "主题变量 key 集合不应为空")
        for key in sorted(keys):
            self.assertNotIn("{" + key + "}", self.shell, f"残留主题占位符 {{{key}}}")

    def test_themes_differ(self):
        light = _get_html_shell("light", "")
        self.assertNotEqual(light, self.shell)

    def test_scroll_layout_present(self):
        # 说明：这是裸 substring 断言，命中当前 shell 中的
        # body（flex-direction: column）+ #ai-history-list（overflow-y: auto），
        # 并非真的在断言"header 固定 + #content 独立滚动"。
        # 三区域布局后 dropdown/list 也含这两个字符串，该测试巧合通过；
        # 真正验证三区域结构的断言见 test_three_zone_layout。
        # 保留本测试仍覆盖消息区滚动结构（overflow-y: auto 仍在 shell 中）。
        self.assertIn("overflow-y: auto", self.shell)
        self.assertIn("flex-direction: column", self.shell)

    def test_three_zone_layout(self):
        # 三区域固定布局（T1 落地）：dropdown flex column + overflow:hidden，
        # list 独立滚动，滚动条迁移到 list。
        # 用 scoped rule-block 断言（正则抽取 #selector{...}），
        # 不能裸 substring——否则会因其他 selector 巧合通过。
        import re

        def block(sel):
            m = re.search(re.escape(sel) + r"\s*\{([^}]*)\}", self.shell)
            return m.group(1) if m else ""

        dd = block("#ai-history-dropdown")
        self.assertIn("overflow: hidden", dd)
        self.assertIn("flex-direction: column", dd)
        self.assertNotIn("overflow-y: auto", dd, "dropdown 不应再整容器滚动")
        li = block("#ai-history-list")
        self.assertIn("overflow-y: auto", li)
        self.assertIn("flex:", li)
        self.assertIn("min-height: 0", li)
        self.assertIn("#ai-history-list::-webkit-scrollbar", self.shell)
        self.assertNotIn("#ai-history-dropdown::-webkit-scrollbar", self.shell,
                         "滚动条应迁移到列表")

    def test_content_marker_present(self):
        # 消息区 marker 必须恰好出现一次且位于 #content 内
        from ai_engine.ai_html_template import _INITIAL_HTML_MARKER
        self.assertEqual(self.shell.count(_INITIAL_HTML_MARKER), 1)

    def test_header_js_functions_defined(self):
        # chat.js 内联后，header 交互函数必须存在
        for fn in (
            "function showHeaderSpinner",
            "function hideHeaderSpinner",
            "function updateHeaderTitle",
            "function closeAIPanel",
            "function toggleHistoryDropdown",
            "function renderHistoryList",
            "function historyAction",
        ):
            self.assertIn(fn, self.shell, f"chat.js 缺少 {fn}")


if __name__ == "__main__":
    unittest.main()
