import unittest
import json
import re
from types import SimpleNamespace

from views.ai_chat_panel import _HeaderTitleBridge


class FakeWebView:
    def __init__(self):
        self.calls = []

    def run_javascript(self, js, *args):
        self.calls.append(js)


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


if __name__ == "__main__":
    unittest.main()
