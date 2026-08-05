#!/usr/bin/env python3
"""背景 ``_on_tool_calls_started`` 隔离回归测试（Phase 1 计划第 2 项）。

缺陷：``_on_tool_calls_started(req_id)`` 不校验回调的 ``req_id`` 是否属于**当前
可见会话的活动流**，一律取消推理/文本 flush 定时器并向 WebView 发送
``finishReasoning();``。后台会话（切走后仍在运行的流）或已被取代/过期的
``req_id`` 一旦进入工具阶段，就会停掉可见会话的 flush 定时器、提前终结可见会话
的 thinking 动画——背景流篡改可见流。

修复方向（docs/plans/ai-streaming-quality-phase1-plan.md 第 2 项）：扫描
``_ai_running_convs`` 反查回调 ``req_id`` 的归属会话，仅当归属会话等于当前可见
会话时才允许取消 flush 定时器与调用 ``finishReasoning()``；背景会话、
未知/被取代的 ``req_id`` 一律为 no-op。

覆盖：
- (a) 前台活动流：req_id 匹配可见会话运行态 → 发送 finishReasoning 且清理两类
  flush 定时器（当前已通过，修复不得破坏该行为）。
- (b) 背景流：req_id 归属非可见会话 → 不发送 finishReasoning、不改动 flush 定时
  器（当前无守卫 → FAIL，核心回归）。
- (c) 未知/被取代 req_id：任何运行态都匹配不到 → 同样 no-op（当前无守卫 →
  FAIL，核心回归）。

复用既有无头假面板模式：``AIChatPanel.__new__`` + 桩属性 + 假 WebView
（同 tests/test_ai_superseded_stream_finish.py）。
"""
import os
import unittest

os.environ.setdefault("GDK_BACKEND", "dummy")  # 无头环境导入 GTK

from views.ai_chat_panel import AIChatPanel


class _FakeWebView:
    def __init__(self):
        self.js_calls = []

    def run_javascript(self, js, *args):
        self.js_calls.append(js)


def _make_panel(**overrides):
    """无 GTK 的假 AIChatPanel：__new__ + 桩属性（test_system_prompt 模式）。

    只桩掉与被测逻辑相关的依赖（WebView + 流状态 + flush 定时器状态），使断言
    落在真实的 ``_on_tool_calls_started`` 上。
    """
    panel = AIChatPanel.__new__(AIChatPanel)
    panel._ai_webview = _FakeWebView()
    panel._ai_running_convs = {}
    panel._ai_conversation_id = None
    panel._ai_request_id = 0
    # 推理/文本两类 flush 定时器状态（_init_streaming_state 的对应字段）
    panel._flush_source_id = 0
    panel._flush_scheduled = False
    panel._reasoning_flush_source_id = 0
    panel._reasoning_flush_scheduled = False
    for key, value in overrides.items():
        setattr(panel, key, value)
    return panel


def _running_state(req_id):
    """``_ai_running_convs`` 中的流运行态（与生产 state dict 同构）。"""
    return {
        "streaming": True,
        "req_id": req_id,
        "messages": [{"role": "user", "content": "问题"}],
        "cancel_event": None,
        "current_assistant_text": "",
        "current_reasoning_text": "",
        "response_div_added": False,
        "ai_markdown_text": "",
    }


def _arm_flush_timers(panel):
    """模拟排期中的推理/文本 flush 定时器（非零 id + scheduled=True）。"""
    panel._flush_source_id = 101
    panel._flush_scheduled = True
    panel._reasoning_flush_source_id = 202
    panel._reasoning_flush_scheduled = True


def _assert_timers_untouched(self, panel):
    """断言两类 flush 定时器状态保持排期原样（未被取消/清理）。"""
    self.assertEqual(panel._flush_source_id, 101, "文本 flush 定时器不得被取消")
    self.assertTrue(panel._flush_scheduled, "文本 flush 排期标记不得被清除")
    self.assertEqual(panel._reasoning_flush_source_id, 202, "推理 flush 定时器不得被取消")
    self.assertTrue(panel._reasoning_flush_scheduled, "推理 flush 排期标记不得被清除")


class TestForegroundActiveToolCallsStarted(unittest.TestCase):
    """(a) 前台活动流：req_id 归属可见会话 → 正常停推理并清理定时器。"""

    def test_foreground_active_emits_finish_reasoning_and_clears_timers(self):
        panel = _make_panel()
        panel._ai_running_convs = {"convA": _running_state(req_id=5)}
        panel._ai_conversation_id = "convA"
        panel._ai_request_id = 5
        _arm_flush_timers(panel)

        AIChatPanel._on_tool_calls_started(panel, 5)

        self.assertIn(
            "finishReasoning();", panel._ai_webview.js_calls,
            "前台活动流必须向 JS 发送 finishReasoning",
        )
        self.assertEqual(panel._flush_source_id, 0, "前台活动流须清理文本 flush 定时器")
        self.assertFalse(panel._flush_scheduled)
        self.assertEqual(panel._reasoning_flush_source_id, 0, "前台活动流须清理推理 flush 定时器")
        self.assertFalse(panel._reasoning_flush_scheduled)


class TestBackgroundToolCallsStartedIsolation(unittest.TestCase):
    """(b) 核心回归：背景流（非可见会话）不得触碰可见流的 UI/定时器。"""

    def test_background_stream_does_not_emit_finish_reasoning(self):
        panel = _make_panel()
        # 可见会话 A 正在运行（req_id=8）；后台会话 B 的流进入工具阶段（req_id=5）
        panel._ai_running_convs = {
            "convA": _running_state(req_id=8),
            "convB": _running_state(req_id=5),
        }
        panel._ai_conversation_id = "convA"
        panel._ai_request_id = 8
        _arm_flush_timers(panel)

        AIChatPanel._on_tool_calls_started(panel, 5)

        self.assertNotIn(
            "finishReasoning();", panel._ai_webview.js_calls,
            "背景流不得向可见 WebView 发送 finishReasoning",
        )
        _assert_timers_untouched(self, panel)

    def test_background_stream_does_not_clear_timers_even_alone(self):
        """仅后台会话在运行、可见会话无活动流时，同样不得触碰定时器/JS。"""
        panel = _make_panel()
        panel._ai_running_convs = {"convB": _running_state(req_id=5)}
        panel._ai_conversation_id = "convA"
        panel._ai_request_id = 0
        _arm_flush_timers(panel)

        AIChatPanel._on_tool_calls_started(panel, 5)

        self.assertNotIn("finishReasoning();", panel._ai_webview.js_calls)
        _assert_timers_untouched(self, panel)


class TestUnknownSupersededReqIdIsolation(unittest.TestCase):
    """(c) 核心回归：任何运行态都匹配不到的 req_id 必须为 no-op。"""

    def test_superseded_req_id_is_noop(self):
        panel = _make_panel()
        # 可见会话 A 的活动流 req_id=7；旧流/被取代的 req_id=5 进入工具阶段
        panel._ai_running_convs = {"convA": _running_state(req_id=7)}
        panel._ai_conversation_id = "convA"
        panel._ai_request_id = 7
        _arm_flush_timers(panel)

        AIChatPanel._on_tool_calls_started(panel, 5)

        self.assertNotIn(
            "finishReasoning();", panel._ai_webview.js_calls,
            "被取代的 req_id 不得发送 finishReasoning",
        )
        _assert_timers_untouched(self, panel)

    def test_unknown_req_id_is_noop(self):
        panel = _make_panel()
        panel._ai_running_convs = {"convA": _running_state(req_id=7)}
        panel._ai_conversation_id = "convA"
        panel._ai_request_id = 7
        _arm_flush_timers(panel)

        AIChatPanel._on_tool_calls_started(panel, 999)

        self.assertNotIn("finishReasoning();", panel._ai_webview.js_calls)
        _assert_timers_untouched(self, panel)

    def test_no_running_state_at_all_is_noop(self):
        panel = _make_panel()
        panel._ai_running_convs = {}
        panel._ai_conversation_id = "convA"
        panel._ai_request_id = 3
        _arm_flush_timers(panel)

        AIChatPanel._on_tool_calls_started(panel, 3)

        self.assertNotIn("finishReasoning();", panel._ai_webview.js_calls)
        _assert_timers_untouched(self, panel)


if __name__ == "__main__":
    unittest.main()
