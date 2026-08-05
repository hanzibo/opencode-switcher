#!/usr/bin/env python3
"""WebView 整页重载守卫测试（Wave3：去冗余 load_html + 主题重建修复）。

背景：Wave1 缓存静态外壳（ai_html_template._get_html_shell），但面板内所有
内容变化仍无条件 ``load_html`` 整页重载；``set_theme`` 又用可能滞后的
``_ai_markdown_text`` 重建且不重置流式容器状态。

本文件覆盖（全部无需显示环境/WebKit 运行时）：
- 纯决策函数：``_webview_shell_fingerprint`` 与 ``_should_full_reload_webview``
  （webview 不可用 / 被 suspend / 文档未就绪 / 外壳指纹变化 → 必须完整重载；
  仅存活、就绪且指纹一致 → 可跳过）。
- ``_load_webview_html`` 接线：同外壳存活就绪 DOM 走 in-place ``updateContent``
  并跳过 ``load_html``；从未装载 / 装载未完成（readiness 竞态）/ 主题变化 /
  suspend / force 走完整重载。
- ``_on_webview_load_changed`` 状态机：FINISHED → 就绪，其余事件 → 装载中。
- ``_reset_streaming_dom_state``：DOM 重建后流式容器与回复 div 标记失效。
"""
import unittest

from gi.repository import WebKit2

from views.ai_chat_panel import (
    AIChatPanel,
    _webview_shell_fingerprint,
    _should_full_reload_webview,
)

DARK = ("dark", "pyg-dark")
LIGHT = ("light", "pyg-light")


class TestWebviewShellFingerprint(unittest.TestCase):
    def test_fingerprint_shape(self):
        self.assertEqual(_webview_shell_fingerprint("dark", "pyg"), ("dark", "pyg"))

    def test_fingerprint_differs_by_theme(self):
        self.assertNotEqual(
            _webview_shell_fingerprint("dark", "pyg"),
            _webview_shell_fingerprint("light", "pyg"),
        )

    def test_fingerprint_differs_by_pygments(self):
        self.assertNotEqual(
            _webview_shell_fingerprint("dark", "pyg-a"),
            _webview_shell_fingerprint("dark", "pyg-b"),
        )


class TestShouldFullReloadWebview(unittest.TestCase):
    """完整重载判定：只有「DOM 存活 + 未被 suspend + 文档就绪 + 指纹一致」才可跳过。"""

    def test_not_live_requires_reload(self):
        self.assertTrue(_should_full_reload_webview(DARK, DARK, False, False, True))

    def test_missing_webview_requires_reload(self):
        self.assertTrue(_should_full_reload_webview(None, DARK, False, False, True))

    def test_suspended_requires_reload_even_with_matching_fingerprint(self):
        self.assertTrue(_should_full_reload_webview(DARK, DARK, True, True, True))

    def test_pending_load_requires_reload(self):
        """readiness 竞态：指纹一致但上一轮装载未完成 → 必须整页重载。"""
        self.assertTrue(_should_full_reload_webview(DARK, DARK, True, False, False))

    def test_pending_load_requires_reload_even_when_suspended(self):
        self.assertTrue(_should_full_reload_webview(DARK, DARK, True, True, False))

    def test_never_loaded_requires_reload(self):
        self.assertTrue(_should_full_reload_webview(None, DARK, True, False, True))

    def test_theme_changed_requires_reload(self):
        self.assertTrue(_should_full_reload_webview(DARK, LIGHT, True, False, True))

    def test_pygments_changed_requires_reload(self):
        self.assertTrue(
            _should_full_reload_webview(("dark", "pyg-a"), ("dark", "pyg-b"), True, False, True)
        )

    def test_identical_live_shell_allows_inplace_update(self):
        self.assertFalse(_should_full_reload_webview(DARK, DARK, True, False, True))


class _FakeWebView:
    """记录 load_html / run_javascript 调用的假 WebView。"""

    def __init__(self):
        self.loaded = []
        self.js_calls = []

    def load_html(self, html, base_uri):
        self.loaded.append(html)

    def run_javascript(self, js, *args):
        self.js_calls.append(js)


class _FakePanel:
    """无 GTK 的假面板：只提供 _load_webview_html 依赖的属性/方法。

    _webview_dom_live / _reset_streaming_dom_state 委托真实实现，
    确保被测试的是面板实际逻辑而非测试替身。
    """

    def __init__(self, theme="dark", pygments_css="", loaded_fingerprint=None,
                 suspended=False, ready=True, webview=None):
        self._theme = theme
        self._pygments_css_cache = {theme: pygments_css}
        self._loaded_shell_fingerprint = loaded_fingerprint
        self._webview_suspended = suspended
        self._webview_ready = ready
        self._ai_webview = webview if webview is not None else _FakeWebView()
        self._streaming_container_created = True
        self._ai_response_div_added = True
        self._ai_running_convs = {
            "convA": {"streaming": True, "req_id": 3, "response_div_added": True},
        }
        self._ai_conversation_id = "convA"
        self._ai_html_cache = {}

    def _get_pygments_css(self, theme):
        return self._pygments_css_cache.get(theme, "")

    def get_html_template(self, theme, initial_html=""):
        return f"<html><body class='{theme}'>{initial_html}</body></html>"

    def _webview_dom_live(self):
        return AIChatPanel._webview_dom_live(self)

    def _reset_streaming_dom_state(self):
        AIChatPanel._reset_streaming_dom_state(self)


class TestLoadWebviewHtmlGuard(unittest.TestCase):
    """_load_webview_html 接线：何时跳过整页重载，何时必须重载。"""

    def test_same_live_shell_skips_load_html(self):
        panel = _FakePanel(theme="dark", pygments_css="pyg-dark",
                           loaded_fingerprint=DARK)
        AIChatPanel._load_webview_html(panel, "<p>hi</p>")
        self.assertEqual(panel._ai_webview.loaded, [])
        self.assertEqual(len(panel._ai_webview.js_calls), 1)
        self.assertIn("updateContent", panel._ai_webview.js_calls[0])
        self.assertIn("<p>hi</p>", panel._ai_webview.js_calls[0])
        # DOM 原地替换 → 流式容器/回复 div 标记必须失效
        self.assertFalse(panel._streaming_container_created)
        self.assertFalse(panel._ai_response_div_added)
        self.assertFalse(panel._ai_running_convs["convA"]["response_div_added"])

    def test_never_loaded_does_full_load_and_records_fingerprint(self):
        panel = _FakePanel(theme="dark", pygments_css="pyg-dark",
                           loaded_fingerprint=None)
        AIChatPanel._load_webview_html(panel, "<p>hi</p>")
        self.assertEqual(len(panel._ai_webview.loaded), 1)
        self.assertIn("<p>hi</p>", panel._ai_webview.loaded[0])
        self.assertEqual(panel._loaded_shell_fingerprint, DARK)
        self.assertEqual(panel._ai_webview.js_calls, [])

    def test_theme_change_does_full_load(self):
        panel = _FakePanel(theme="light", pygments_css="pyg-light",
                           loaded_fingerprint=DARK)
        AIChatPanel._load_webview_html(panel, "<p>hi</p>")
        self.assertEqual(len(panel._ai_webview.loaded), 1)
        self.assertIn("light", panel._ai_webview.loaded[0])
        self.assertEqual(panel._loaded_shell_fingerprint, LIGHT)

    def test_suspended_forces_full_load_and_clears_suspend_flag(self):
        panel = _FakePanel(theme="dark", pygments_css="pyg-dark",
                           loaded_fingerprint=DARK, suspended=True)
        AIChatPanel._load_webview_html(panel, "<p>hi</p>")
        self.assertEqual(len(panel._ai_webview.loaded), 1)
        self.assertFalse(panel._webview_suspended)
        self.assertEqual(panel._loaded_shell_fingerprint, DARK)

    def test_force_always_full_loads(self):
        panel = _FakePanel(theme="dark", pygments_css="pyg-dark",
                           loaded_fingerprint=DARK)
        AIChatPanel._load_webview_html(panel, "<p>hi</p>", force=True)
        self.assertEqual(len(panel._ai_webview.loaded), 1)
        self.assertEqual(panel._ai_webview.js_calls, [])

    def test_pending_load_forces_full_load(self):
        """readiness 竞态：指纹一致但文档未就绪 → updateContent 会静默失败，必须整页重载。"""
        panel = _FakePanel(theme="dark", pygments_css="pyg-dark",
                           loaded_fingerprint=DARK, ready=False)
        AIChatPanel._load_webview_html(panel, "<p>hi</p>")
        self.assertEqual(len(panel._ai_webview.loaded), 1)
        self.assertEqual(panel._ai_webview.js_calls, [])
        # 重载已发出 → 就绪标记必须回到 False，等待下一次 FINISHED
        self.assertFalse(panel._webview_ready)
        self.assertEqual(panel._loaded_shell_fingerprint, DARK)

    def test_full_load_branch_marks_not_ready(self):
        panel = _FakePanel(theme="dark", pygments_css="pyg-dark",
                           loaded_fingerprint=DARK, ready=True)
        AIChatPanel._load_webview_html(panel, "<p>hi</p>", force=True)
        self.assertEqual(len(panel._ai_webview.loaded), 1)
        self.assertFalse(panel._webview_ready)

    def test_load_changed_finished_marks_ready(self):
        panel = _FakePanel(theme="dark", pygments_css="pyg-dark",
                           loaded_fingerprint=DARK, ready=False)
        AIChatPanel._on_webview_load_changed(panel, None, WebKit2.LoadEvent.FINISHED)
        self.assertTrue(panel._webview_ready)
        AIChatPanel._on_webview_load_changed(panel, None, WebKit2.LoadEvent.STARTED)
        self.assertFalse(panel._webview_ready)
        AIChatPanel._on_webview_load_changed(panel, None, WebKit2.LoadEvent.FINISHED)
        self.assertTrue(panel._webview_ready)

    def test_pending_then_finished_then_inplace_update(self):
        """端到端状态机：装载未完成 → 整页重载；FINISHED 后就绪 → 同外壳走 updateContent。"""
        panel = _FakePanel(theme="dark", pygments_css="pyg-dark",
                           loaded_fingerprint=DARK, ready=False)
        AIChatPanel._load_webview_html(panel, "<p>first</p>")
        self.assertEqual(len(panel._ai_webview.loaded), 1)
        self.assertFalse(panel._webview_ready)

        AIChatPanel._on_webview_load_changed(panel, None, WebKit2.LoadEvent.FINISHED)
        self.assertTrue(panel._webview_ready)

        AIChatPanel._load_webview_html(panel, "<p>second</p>")
        self.assertEqual(len(panel._ai_webview.loaded), 1)  # 未再整页重载
        self.assertEqual(len(panel._ai_webview.js_calls), 1)
        self.assertIn("updateContent", panel._ai_webview.js_calls[0])
        self.assertIn("<p>second</p>", panel._ai_webview.js_calls[0])

    def test_same_theme_different_pygments_full_loads(self):
        panel = _FakePanel(theme="dark", pygments_css="pyg-new",
                           loaded_fingerprint=("dark", "pyg-old"))
        AIChatPanel._load_webview_html(panel, "<p>hi</p>")
        self.assertEqual(len(panel._ai_webview.loaded), 1)
        self.assertEqual(panel._loaded_shell_fingerprint, ("dark", "pyg-new"))

    def test_empty_content_still_skips_on_same_live_shell(self):
        panel = _FakePanel(theme="dark", pygments_css="pyg-dark",
                           loaded_fingerprint=DARK)
        AIChatPanel._load_webview_html(panel, "")
        self.assertEqual(panel._ai_webview.loaded, [])
        self.assertIn('updateContent("")', panel._ai_webview.js_calls[0])


class TestResetStreamingDomState(unittest.TestCase):
    def test_resets_panel_and_active_conv_flags(self):
        panel = _FakePanel(theme="dark", pygments_css="pyg-dark",
                           loaded_fingerprint=DARK)
        AIChatPanel._reset_streaming_dom_state(panel)
        self.assertFalse(panel._streaming_container_created)
        self.assertFalse(panel._ai_response_div_added)
        self.assertFalse(panel._ai_running_convs["convA"]["response_div_added"])

    def test_does_not_touch_background_convs(self):
        panel = _FakePanel(theme="dark", pygments_css="pyg-dark",
                           loaded_fingerprint=DARK)
        panel._ai_running_convs["convB"] = {"streaming": True, "req_id": 9,
                                            "response_div_added": True}
        AIChatPanel._reset_streaming_dom_state(panel)
        self.assertTrue(panel._ai_running_convs["convB"]["response_div_added"])


if __name__ == "__main__":
    unittest.main()
