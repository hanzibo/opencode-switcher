#!/usr/bin/env python3
"""opencode://history-close 协议分支：关闭历史下拉后归还焦点到输入框。

覆盖（T9：_on_decide_policy 新增 history-close 分支）：
- (a) 处理 ``opencode://history-close`` → handler 返回 True，``decision.ignore()``
  被调用，且 ``self._ai_entry.grab_focus`` 经 ``GLib.idle_add`` 调度（GTK 主线程
  归还焦点，不直接跨线程触碰 UI）。
- (b) 既有 opencode:// 分支不受影响：``opencode://history-open`` 只刷新/展开下拉，
  不触发 grab_focus。
- (c) 非 NAVIGATION_ACTION 的决策类型直接返回 False（协议只处理导航动作）。

复用既有无头假面板模式：``AIChatPanel.__new__`` + 桩属性 + 假 decision
（duck-type ``ignore()``/``get_navigation_action()``，同 test_ai_switch_unsaved_running）。
GLib.idle_add 在无主循环时不执行回调：测试 patch 掉 ``GLib.idle_add`` 捕获回调后
手动执行，断言 grab_focus 被调用。
"""
import os
import unittest
from unittest import mock

os.environ.setdefault("GDK_BACKEND", "dummy")  # 无头环境导入 GTK

import gi
gi.require_version("WebKit2", "4.1")  # 与 ai_chat_panel 保持一致，先于仓库加载
gi.require_version("PangoCairo", "1.0")  # ai_chat_panel 模块导入会带出 PangoCairo
from gi.repository import GLib, WebKit2
from views.ai_chat_panel import AIChatPanel

NAVIGATION_ACTION = WebKit2.PolicyDecisionType.NAVIGATION_ACTION


# ═══════════════════════════════════════════════════════════════════
#  无头假件（display-independent）
# ═══════════════════════════════════════════════════════════════════


class _FakeEntry:
    """带 mock grab_focus 的假输入框（真实面板里 _ai_entry 是 Gtk.TextView）。"""

    def __init__(self):
        self.grab_focus = mock.Mock()


class _FakeWebView:
    """记录 run_javascript 调用的假 WebView（history-open 分支依赖）。"""

    def __init__(self):
        self.js_calls = []

    def run_javascript(self, js, *args):
        self.js_calls.append(js)


class _FakeRequest:
    def __init__(self, uri):
        self._uri = uri

    def get_uri(self):
        return self._uri


class _FakeNavAction:
    def __init__(self, uri):
        self._request = _FakeRequest(uri)

    def get_request(self):
        return self._request


class _FakeDecision:
    """duck-type 真实 WebKit NavigationPolicyDecision：记录 ignore() 调用。"""

    def __init__(self, uri):
        self.ignored = False
        self._nav_action = _FakeNavAction(uri)

    def get_navigation_action(self):
        return self._nav_action

    def ignore(self):
        self.ignored = True


def _make_panel(**overrides):
    """无 GTK 的假 AIChatPanel：__new__ + 仅含协议分支所需的最小桩。"""
    panel = AIChatPanel.__new__(AIChatPanel)
    panel._ai_entry = _FakeEntry()
    panel._ai_webview = _FakeWebView()
    panel._ai_history_popover = mock.Mock()
    for key, value in overrides.items():
        setattr(panel, key, value)
    return panel


def _handle(panel, uri, decision_type=NAVIGATION_ACTION):
    """直接调用 _on_decide_policy，返回 (返回值, decision)。"""
    decision = _FakeDecision(uri)
    ret = AIChatPanel._on_decide_policy(panel, None, decision, decision_type)
    return ret, decision


# ═══════════════════════════════════════════════════════════════════
#  (a) history-close：归还焦点到输入框
# ═══════════════════════════════════════════════════════════════════


class TestHistoryCloseProtocol(unittest.TestCase):
    """history-close 协议分支：GLib.idle_add 调度 grab_focus + 返回 True。"""

    def test_history_close_refocuses_input(self):
        """处理 history-close：grab_focus 必须经 idle_add 调度且最终被调用。"""
        panel = _make_panel()
        entry = panel._ai_entry
        captured = []

        with mock.patch.object(
            GLib, "idle_add", side_effect=lambda cb, *a: captured.append((cb, a))
        ) as mock_idle_add:
            ret, decision = _handle(panel, "opencode://history-close")

        # 协议已消费：返回 True + decision.ignore()
        self.assertTrue(ret, "history-close 必须返回 True（导航已被消费）")
        self.assertTrue(decision.ignored, "opencode:// 导航必须调用 decision.ignore()")
        # 焦点归还经 GLib.idle_add 调度（不得在 decide-policy 回调里直接碰 UI）
        self.assertEqual(
            mock_idle_add.call_count, 1,
            "history-close 必须恰好调度一次 idle_add",
        )
        self.assertIs(captured[0][0], entry.grab_focus,
                      "idle_add 必须调度 _ai_entry.grab_focus")
        # 无主循环不执行回调 → 手动执行调度项，验证 grab_focus 真正被调用
        cb, args = captured[0]
        cb(*args)
        entry.grab_focus.assert_called_once()  # grab_focus() 无参数
    def test_history_close_prefix_safe(self):
        """前缀安全：history-close 不得被 history-select/delete 分支短路。"""
        panel = _make_panel()
        with mock.patch.object(GLib, "idle_add", side_effect=lambda cb, *a: cb(*a)):
            ret, decision = _handle(panel, "opencode://history-close?x=1")
        self.assertTrue(ret)
        self.assertTrue(decision.ignored)
        panel._ai_entry.grab_focus.assert_called_once()


# ═══════════════════════════════════════════════════════════════════
#  (b) 既有分支不受影响（守卫）
# ═══════════════════════════════════════════════════════════════════


class TestExistingProtocolsUntouched(unittest.TestCase):
    """既有 opencode:// 分支不得触发 grab_focus 或被 history-close 影响。"""

    def test_history_open_does_not_grab_focus(self):
        """history-open 只刷新/展开下拉：不调度 grab_focus。"""
        panel = _make_panel()
        with mock.patch.object(GLib, "idle_add") as mock_idle_add:
            ret, decision = _handle(panel, "opencode://history-open")
        self.assertTrue(ret)
        self.assertTrue(decision.ignored)
        panel._ai_history_popover.refresh_dropdown.assert_called_once()
        self.assertEqual(
            panel._ai_webview.js_calls, ["showHistoryDropdown();"],
            "history-open 必须展开下拉",
        )
        mock_idle_add.assert_not_called()  # history-open 不调度 grab_focus
        panel._ai_entry.grab_focus.assert_not_called()

    def test_non_navigation_decision_returns_false(self):
        """非 NAVIGATION_ACTION 决策类型（如 NEW_WINDOW）直接返回 False。"""
        panel = _make_panel()
        decision = _FakeDecision("opencode://history-close")
        ret = AIChatPanel._on_decide_policy(
            panel, None, decision, WebKit2.PolicyDecisionType.NEW_WINDOW_ACTION,
        )
        self.assertFalse(
            ret, "非导航决策类型必须返回 False（不消费该决策）",
        )
        self.assertFalse(decision.ignored, "非导航决策不得调用 decision.ignore()")


if __name__ == "__main__":
    unittest.main()
