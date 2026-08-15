"""Tests for the WebView-embedded AI header (紫月星云 spinner + 装饰区).

原独立微型 WebView（ai_engine/nebula_spinner.py）已删除——spinner 与 header
全部内联进主 WebView 模板（ai_engine/ai_html_template.py 的 #ai-header），
省掉第二个 WebKitWebProcess。这里验证模板结构与主题变量注入。
"""

import re
import unittest

from ai_engine.ai_html_template import _get_html_shell


def _block(sel, shell):
    """scoped rule-block 抽取：#selector{...} → 块内容（不存在返回空串）。"""
    m = re.search(re.escape(sel) + r"\s*\{([^}]*)\}", shell)
    return m.group(1) if m else ""


def _keyframes(name, shell):
    """keyframe 块抽取：@keyframes <name>{...}（含嵌套帧块）→ 块内容。

    约束：帧内不得含嵌套花括号（当前 {orbit}/{glow} 已被主题变量替换为
    rgba 值，故正则的 [^{}]* 成立；若未来帧内加入媒体查询等结构需同步升级）。
    """
    m = re.search(
        r"@keyframes " + re.escape(name) + r"\s*\{((?:[^{}]*\{[^{}]*\})*[^{}]*)\}",
        shell, re.S,
    )
    return m.group(1) if m else ""


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
        btn = _block("#ai-header .ai-header-btn", self.shell)
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
        dd = _block("#ai-history-dropdown", self.shell)
        self.assertIn("overflow: hidden", dd)
        self.assertIn("flex-direction: column", dd)
        self.assertNotIn("overflow-y: auto", dd, "dropdown 不应再整容器滚动")
        li = _block("#ai-history-list", self.shell)
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

    def test_phantom_clear_deleted_removed(self):
        # 回归守卫（remove-ai-history-clear-button）："清空已删除"是幻影按钮
        # （调 _reset_ai_panel_silent 纯 UI 重置，有数据丢失副作用），按钮行、
        # historyAction('clear') 接线与 history-clear CSS/JS 引用均已永久移除。
        # 断言必须命中真实模板/渲染产物——chat.js 已内联进 shell，故四断言均查 self.shell。
        # 若未来重新引入该按钮，此测试立即红灯。
        self.assertNotIn("清空已删除", self.shell, "幻影按钮'清空已删除'已移除，不应出现在模板中")
        self.assertNotIn("historyAction('clear')", self.shell, "clear 接线已移除，不应出现在 chat.js 中")
        self.assertNotIn("history-clear", self.shell, "history-clear CSS/JS 引用已移除")
        # 编辑按钮保留的回归锚点：historyAction 函数仍被 edit 按钮使用
        self.assertIn("historyAction('edit')", self.shell, "编辑按钮保留，edit 接线应存在")

    def test_theme_accessor_key_sets_consistent(self):
        # T5 键集硬化：3 主题 × 各 accessor 键集跨主题一致 + 无 KeyError
        # （未来某主题漏配键，accessor 抛 KeyError 或键集不一致即触发）
        from stores.theme_config import (
            _THEMES, get_ai_spinner_vars, get_ai_gtk_colors, get_panel_css_vals,
        )
        themes = list(_THEMES)
        self.assertEqual(len(themes), 3)
        for accessor in (get_ai_spinner_vars, get_ai_gtk_colors, get_panel_css_vals):
            base = frozenset(accessor(themes[0]))
            self.assertTrue(base, f"{accessor.__name__}({themes[0]}) 返回空键集")
            for name in themes[1:]:
                self.assertEqual(
                    frozenset(accessor(name)), base,
                    f"{accessor.__name__}({name}) 键集与 {themes[0]} 不一致",
                )

    def test_history_dropdown_high_opacity_background(self):
        # T5 M2：history 下拉必须带高不透明背景（毛玻璃回退），按主题静态断言
        # 字符串（非 computed style，避免 headless flaky）。
        expected = {
            "dark": "rgba(10,11,16,0.92)",
            "dark-moon": "rgba(15,9,20,0.92)",
            "light": "rgba(255,255,255,0.97)",
        }
        for theme, color in expected.items():
            shell = _get_html_shell(theme, "")
            rule = _block(".%s #ai-history-dropdown" % theme, shell)
            self.assertIn(
                "background-color: %s" % color, rule,
                f".{theme} #ai-history-dropdown 缺少高不透明背景 {color}",
            )

    def test_content_base_rule_has_breathing_prep(self):
        # T3: #content 基规则必须预置呼吸灯配套——box-sizing 防加边框后底部裁切、
        # 永久透明边框（宽度恒定防淡出 snap）、底部不亮、transition 0.3s 渐隐
        base = _block("#content", self.shell)
        self.assertIn("box-sizing: border-box", base, "#content 缺少 box-sizing: border-box")
        self.assertIn("border: 2px solid transparent", base, "#content 缺少透明边框占位")
        self.assertIn("border-bottom: none", base, "#content 底部不应亮")

    def test_content_streaming_glow_per_theme(self):
        # T3: .streaming-glow 块按主题注入 {glow} 边框 + 2.4s 呼吸动画。
        # border-bottom:none 位于基规则 #content 块内（test_content_base_rule_has_breathing_prep
        # 已覆盖），此处不重复断言。
        from stores.theme_config import _THEMES
        expected = {
            "light": "#7c3aed",
            "dark": "#8b5cf6",
            "dark-moon": "#a855f7",
        }
        self.assertEqual(set(expected), set(_THEMES), "期望 hex 映射必须覆盖全部 3 主题")
        for theme, hex_color in expected.items():
            rule = _block("#content.streaming-glow", _get_html_shell(theme, ""))
            self.assertIn(
                "border-color: %s" % hex_color, rule,
                f"{theme} #content.streaming-glow 缺少 glow 边框 {hex_color}",
            )
            self.assertIn(
                "animation: breathing-glow 2.4s", rule,
                f"{theme} #content.streaming-glow 缺少 2.4s 呼吸动画",
            )

    def test_breathing_glow_js_and_keyframes(self):
        # T3: keyframe 必须存在；chat.js setStreamingGlow 契约——spinner 显示 == 流式中，
        # showHeaderSpinner/hideHeaderSpinner 均驱动发光（覆盖发送/重试/工具循环/取消/错误全路径）。
        # 熄灭硬切修复（T2）：setStreamingGlow(false) add glow-fading 走专用淡出动画，
        # _glowFadeTimer 防抖驱动（true 分支 clearTimeout 防止连续 toggle 残留）
        self.assertIn("@keyframes breathing-glow", self.shell)
        self.assertIn("function setStreamingGlow", self.shell)
        self.assertIn("classList.add('streaming-glow')", self.shell, "setStreamingGlow(true) 应 add streaming-glow")
        self.assertIn("classList.add('glow-fading')", self.shell, "setStreamingGlow(false) 应 add glow-fading")
        self.assertIn("_glowFadeTimer = setTimeout", self.shell, "淡出应由 _glowFadeTimer 驱动")
        self.assertIn("clearTimeout(_glowFadeTimer)", self.shell, "true 分支应 clearTimeout 防抖")
        show = re.search(r"function showHeaderSpinner\(.*?\n}", self.shell, re.S)
        hide = re.search(r"function hideHeaderSpinner\(.*?\n}", self.shell, re.S)
        self.assertTrue(show and hide, "showHeaderSpinner/hideHeaderSpinner 函数缺失")
        self.assertIn("setStreamingGlow(true)", show.group(0), "showHeaderSpinner 应开启呼吸灯")
        self.assertIn("setStreamingGlow(false)", hide.group(0), "hideHeaderSpinner 应关闭呼吸灯")
        # 三段呼吸 keyframe 帧断言：0%/50%/100% + 谷底 6px / 峰值 28px
        # （{orbit} 已被主题变量替换为实际颜色，故只断言光晕半径）
        frames = _keyframes("breathing-glow", self.shell)
        self.assertTrue(frames, "breathing-glow 块缺失")
        for pct in ("0%", "50%", "100%"):
            self.assertIn(pct, frames, f"breathing-glow 缺少 {pct} 帧")
        self.assertIn("inset 0 0 6px", frames, "谷底应为 6px")
        self.assertIn("inset 0 0 28px", frames, "峰值应为 28px")

    def test_glow_fadeout_animation(self):
        # 熄灭硬切修复：专用淡出动画（glow-fadeout 350ms forwards）——动画内渐隐，
        # 不依赖 transition（WebKit 对运行中动画移除的过渡不可靠）；JS 侧 350ms 后移除 class
        self.assertIn("@keyframes glow-fadeout", self.shell, "glow-fadeout keyframe 缺失")
        fading = _block("#content.glow-fading", self.shell)
        self.assertIn(
            "animation: glow-fadeout 350ms ease-out forwards", fading,
            "#content.glow-fading 应为 350ms ease-out forwards 淡出动画",
        )
        fadeout = _keyframes("glow-fadeout", self.shell)
        self.assertTrue(fadeout, "glow-fadeout 块缺失")
        self.assertIn("inset 0 0 28px", fadeout, "from 帧应为峰值 28px")
        self.assertIn("inset 0 0 0", fadeout, "to 帧应回落 0 光晕")
        self.assertIn("border-color: transparent", fadeout, "边框色应淡出至透明")


if __name__ == "__main__":
    unittest.main()
