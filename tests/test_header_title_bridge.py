import os
import unittest
import json
import re
from types import SimpleNamespace
from unittest import mock

os.environ.setdefault("GDK_BACKEND", "dummy")  # 无头环境导入 GTK

import gi
gi.require_version("WebKit2", "4.1")  # 与 ai_chat_panel 保持一致，先于仓库加载
gi.require_version("PangoCairo", "1.0")  # ai_chat_panel 模块导入会带出 PangoCairo
from gi.repository import WebKit2

from views.ai_chat_panel import _HeaderTitleBridge, AIChatPanel
from tests._helpers import FakeWebView


class TestHeaderTitleBridge(unittest.TestCase):

    def setUp(self):
        self.wv = FakeWebView()
        self.bridge = _HeaderTitleBridge(SimpleNamespace(_ai_webview=self.wv))

    def _last_js(self):
        return self.wv.calls[-1]

    def test_normal_split(self):
        markup = (
            "<b>AI 助手看盘</b>\n"
            "<span size='small' foreground='#888888'>(DeepSeek V4 Flash(go) (deepseek-v4-flash))</span>"
        )
        self.bridge.set_markup(markup)
        expected = (
            'updateHeaderTitle("<b>AI 助手看盘</b>", '
            '"(DeepSeek V4 Flash(go) (deepseek-v4-flash))");'
        )
        self.assertEqual(self._last_js(), expected)

    def test_abort_variant(self):
        # 真实中止 markup（ai_chat_panel.py:2766）带 <b> 标题，颜色属性 f43f5e 只存在于
        # span 标签上——title 走 <b> 分支后 f43f5e 不应泄漏进 JS 字符串
        markup = (
            "<b>AI 助手看盘</b>\n"
            "<span size='small' foreground='#f43f5e'>(正在中止...)</span>"
        )
        self.bridge.set_markup(markup)
        expected = (
            'updateHeaderTitle("<b>AI 助手看盘</b>", '
            '"(正在中止...)");'
        )
        self.assertEqual(self._last_js(), expected)
        self.assertNotIn("f43f5e", self._last_js())

    def test_escaping(self):
        markup = (
            "<b>AI 助手看盘</b>\n"
            "<span size='small' foreground='#888888'>(C++ \"v2\" \\path)</span>"
        )
        self.bridge.set_markup(markup)
        js = self._last_js()
        expected = 'updateHeaderTitle("<b>AI 助手看盘</b>", %s);' % json.dumps(
            '(C++ "v2" \\path)', ensure_ascii=False
        )
        self.assertEqual(js, expected)
        # 转义完整性：未转义的原始 payload 不应裸出现在 JS 中
        self.assertNotIn('(C++ "v2" \\path)', js)
        # 安全回读：JS 两个参数用 json.loads 能还原出原始 title/model
        m = re.match(r"^updateHeaderTitle\((.*), (.*)\);$", js, re.S)
        self.assertIsNotNone(m)
        title_back = json.loads(m.group(1))
        model_back = json.loads(m.group(2))
        self.assertEqual(title_back, "<b>AI 助手看盘</b>")
        self.assertEqual(model_back, '(C++ "v2" \\path)')

    def test_no_span(self):
        markup = "<b>AI 助手看盘</b>"
        self.bridge.set_markup(markup)
        expected = 'updateHeaderTitle("<b>AI 助手看盘</b>", "");'
        self.assertEqual(self._last_js(), expected)

    def test_no_b(self):
        markup = "plain text"
        self.bridge.set_markup(markup)
        # 无 <b> 标签 → title 参数回退为整个 markup
        expected = 'updateHeaderTitle("plain text", "");'
        self.assertEqual(self._last_js(), expected)

    def test_none_webview(self):
        bridge = _HeaderTitleBridge(SimpleNamespace(_ai_webview=None))
        # 无 webview → 静默返回，不抛异常、无 JS 调用
        result = bridge.set_markup("<b>AI 助手看盘</b>\n<span>(model)</span>")
        self.assertIsNone(result)
        self.assertEqual(self.wv.calls, [])

    def test_resume_reapply(self):
        # T3 resume 场景：on_panel_shown 整页重建后重新应用最近一次 markup
        normal_markup = (
            "<b>AI 助手看盘</b>\n"
            "<span size='small' foreground='#888888'>(DeepSeek V4 Flash(go) (deepseek-v4-flash))</span>"
        )
        # 1. set_markup 存储 markup
        self.bridge.set_markup(normal_markup)
        self.assertEqual(self.bridge._ai_last_header_markup, normal_markup)
        first = self.wv.calls[0]
        # 2. 模拟 on_panel_shown 重新应用分支 → 第二次 JS 与第一次完全相同
        self.bridge.set_markup(self.bridge._ai_last_header_markup)
        self.assertEqual(len(self.wv.calls), 2)
        self.assertEqual(self.wv.calls[1], first)
        # 3. 空历史守卫：全新 bridge 从未 set_markup → None，分支跳过，零 JS 调用
        fresh_wv = FakeWebView()
        fresh_bridge = _HeaderTitleBridge(SimpleNamespace(_ai_webview=fresh_wv))
        self.assertIsNone(fresh_bridge._ai_last_header_markup)
        last = getattr(fresh_bridge, "_ai_last_header_markup", None)
        if last:
            fresh_bridge.set_markup(last)
        self.assertEqual(fresh_wv.calls, [])

    def test_resume_finished_flag(self):
        """T5 M2 真调用：resume 整页重建 → FINISHED 消费 flag → 恰好一次重放 + 复位。

        __new__ 构造面板桩（不调真实 __init__/WebKit），mock _load_webview_html，
        驱动真实 on_panel_shown resume 分支 + 真实 _on_webview_load_changed(FINISHED)：
        load_html 完成前不得 run_javascript（重建 DOM 会丢弃调用），FINISHED 后
        恰好一次 updateHeaderTitle 重放且 flag 复位。
        """
        wv = FakeWebView()
        bridge = _HeaderTitleBridge(SimpleNamespace(_ai_webview=wv))
        panel = AIChatPanel.__new__(AIChatPanel)
        panel._ai_lbl = bridge
        panel._ai_webview = wv
        panel._init_mcp = mock.Mock()
        panel._suspend_timeout_id = 0
        panel._webview_suspended = True
        panel._ai_html_cache = {}
        panel._ai_conversation_id = "conv1"
        panel._ai_entry = mock.Mock()
        panel._load_webview_html = mock.Mock()

        markup = (
            "<b>AI 助手看盘</b>\n"
            "<span size='small' foreground='#888888'>(DeepSeek V4 Flash(go) (deepseek-v4-flash))</span>"
        )
        # 初始 set_markup：1 次 JS 调用 + 记录最近 markup；flag 初始 False
        bridge.set_markup(markup)
        self.assertEqual(len(wv.calls), 1)
        self.assertIs(bridge._pending_header_reapply, False)

        # resume 分支（on_panel_shown）：登记待重放 flag + 触发整页重建
        panel.on_panel_shown()
        self.assertIs(bridge._pending_header_reapply, True,
                      "resume 分支必须登记待重放 flag")
        panel._load_webview_html.assert_called_once()
        self.assertEqual(len(wv.calls), 1,
                         "load_html 完成前不得 run_javascript（重建 DOM 会丢弃）")

        # FINISHED 事件消费 flag：恰好一次 updateHeaderTitle 重放 + flag 复位
        panel._on_webview_load_changed(wv, WebKit2.LoadEvent.FINISHED)
        self.assertIs(panel._webview_ready, True, "FINISHED 必须标记 webview 就绪")
        self.assertIs(bridge._pending_header_reapply, False,
                      "FINISHED 必须消费并复位待重放 flag")
        self.assertEqual(len(wv.calls), 2, "FINISHED 后必须恰好一次标题重放")
        self.assertEqual(wv.calls[1], wv.calls[0], "重放 JS 必须与初始调用一致")

    def test_load_changed_non_finished_resets_ready(self):
        """非 FINISHED 事件（PROVISIONAL/COMMITTED）必须将 _webview_ready 置 False。"""
        wv = FakeWebView()
        bridge = _HeaderTitleBridge(SimpleNamespace(_ai_webview=wv))
        panel = AIChatPanel.__new__(AIChatPanel)
        panel._ai_lbl = bridge
        panel._webview_ready = True
        panel._on_webview_load_changed(wv, WebKit2.LoadEvent.COMMITTED)
        self.assertIs(panel._webview_ready, False,
                      "非 FINISHED 事件不得保持就绪态")
        self.assertEqual(wv.calls, [], "装载中事件不得触发标题 JS")


if __name__ == "__main__":
    unittest.main()
