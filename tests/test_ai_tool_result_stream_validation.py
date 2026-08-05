#!/usr/bin/env python3
"""``_on_tool_result`` 流式 req_id 归属校验 RED 回归测试。

已知 bug（本文件为纯测试，不修改生产代码）：
``views/ai_chat_panel.py`` 的 ``_on_tool_result`` 把回调携带的 ``req_id`` 与**全局
计数器** ``self._ai_request_id`` 判等（约 1010 行）。A→B→A 切回后全局
``_ai_request_id`` 已随每次切换递增（=7），而当前可见会话仍在运行的流实例
``req_id`` 仍为 5 → 合法的前台流工具结果被当作过期请求丢弃，``updateToolCard``
永不执行，工具卡片停留在「正在运行中...」状态。

修复方向（docs/plans/ai-streaming-quality-phase1-plan.md 步骤 3）：改为按
``_ai_running_convs`` 反查流归属（per-stream ``req_id``）+ 可见会话校验；仅
**前台可见会话的活跃流**接受自身工具结果，背景 / 未知 / 非流式状态一律拒绝。

覆盖：
- (a) 前台切回（rebound）流接受自身工具结果 → ``updateToolCard`` 执行
  （核心回归，当前 ``req_id(5) != 全局(7)`` → 被丢弃 → FAIL）。
- (b) 未知 req_id 拒绝：无任何运行态流匹配 → 不更新 DOM（当前通过）。
- (c) 背景会话拒绝：req_id 归属非当前可见会话 → 不更新 DOM（当前通过）。
- (d) 非流式状态拒绝：可见会话运行态 ``streaming=False`` → 不更新 DOM（当前通过）。
- (e) 基架校验：``enable_incremental_tools=False`` 时直接短路（当前通过）。

复用既有无头假面板模式：``AIChatPanel.__new__`` + 桩属性 + 假 WebView
（同 tests/test_ai_superseded_stream_finish.py）。
"""
import os
import unittest
from types import SimpleNamespace
from unittest import mock

os.environ.setdefault("GDK_BACKEND", "dummy")  # 无头环境导入 GTK

from views.ai_chat_panel import AIChatPanel

# 夹具常量：工具调用定义 + 运行态流 req_id + 递增后的全局 req_id
TOOL_CALL_ID = "call_abc123"
RESULT_TEXT = "搜索完成：找到 3 条结果"
STATUS_OK = "success"
STREAM_REQ_ID = 5
GLOBAL_REQ_ID = 7  # A→B→A 切回后全局计数器已递增


# ═══════════════════════════════════════════════════════════════════
#  无头假件（display-independent，同 test_ai_superseded_stream_finish）
# ═══════════════════════════════════════════════════════════════════


class _FakeWebView:
    def __init__(self):
        self.js_calls = []

    def run_javascript(self, js, *args):
        self.js_calls.append(js)


def _make_panel(**overrides):
    """无 GTK 的假 AIChatPanel：__new__ + 桩属性。

    只桩掉与 ``_on_tool_result`` 断言无关的属性；被测逻辑（``_find_tool_call_by_id``
    与归属校验）落在真实实现上。``enable_incremental_tools`` 默认为 True 以进入
    被测分支。
    """
    panel = AIChatPanel.__new__(AIChatPanel)
    panel._ai_webview = _FakeWebView()
    panel._ai_running_convs = {}
    panel._ai_request_id = GLOBAL_REQ_ID
    panel._ai_conversation_id = None
    panel._ai_messages = []
    panel._ai_settings_store = SimpleNamespace(
        enable_incremental_tools=True,
        show_tool_details=True,
    )
    for key, value in overrides.items():
        setattr(panel, key, value)
    return panel


def _assistant_tool_call_messages():
    """含一条 assistant tool_calls 消息的消息列表（供 _find_tool_call_by_id 命中）。"""
    return [
        {"role": "user", "content": "帮我搜索一下"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": TOOL_CALL_ID,
                    "type": "function",
                    "function": {"name": "web_search", "arguments": "{}"},
                }
            ],
        },
    ]


def _stream_state(req_id, streaming=True):
    """单个会话的运行态流状态（镜像 _run_llm_api_request 写入的结构）。"""
    return {
        "streaming": streaming,
        "req_id": req_id,
        "request_key": ("ai", "convX", req_id),
        "cancel_event": mock.Mock(),
        "messages": [{"role": "user", "content": "问题"}],
        "current_assistant_text": "",
        "current_reasoning_text": "",
        "response_div_added": False,
        "ai_markdown_text": "",
    }


def _js_calls(panel):
    return panel._ai_webview.js_calls


# ═══════════════════════════════════════════════════════════════════
#  (a) 核心回归：前台切回（rebound）流接受自身工具结果
# ═══════════════════════════════════════════════════════════════════


class TestForegroundReboundAcceptance(unittest.TestCase):
    """A→B→A 切回后，可见会话仍在运行的流（req_id=5）必须接受自身工具结果。"""

    def _rebound_foreground_panel(self):
        panel = _make_panel()
        st = _stream_state(req_id=STREAM_REQ_ID, streaming=True)
        panel._ai_running_convs = {"convA": st}
        panel._ai_conversation_id = "convA"  # A 是当前可见会话
        panel._ai_messages = _assistant_tool_call_messages()
        return panel, st

    def test_rebound_foreground_tool_result_updates_tool_card(self):
        """可见会话的活跃流回调 req_id=5（全局已 7）→ updateToolCard 必须执行。"""
        panel, _st = self._rebound_foreground_panel()

        AIChatPanel._on_tool_result(panel, TOOL_CALL_ID, RESULT_TEXT, STATUS_OK, STREAM_REQ_ID)

        # 归属校验通过 → JS 调用恰为一次 updateToolCard（含工具卡片 HTML）
        calls = _js_calls(panel)
        self.assertEqual(len(calls), 1, "前台可见流的工具结果必须执行 updateToolCard")
        self.assertIn("updateToolCard", calls[0])
        self.assertIn(TOOL_CALL_ID, calls[0])

    def test_rebound_foreground_uses_matching_webview_even_if_global_differs(self):
        """核心断言：req_id=5（可见流）即使 != 全局 7 也须更新 DOM。"""
        panel, _st = self._rebound_foreground_panel()
        self.assertEqual(panel._ai_request_id, GLOBAL_REQ_ID)

        AIChatPanel._on_tool_result(panel, TOOL_CALL_ID, RESULT_TEXT, STATUS_OK, STREAM_REQ_ID)

        self.assertTrue(
            any("updateToolCard" in js for js in _js_calls(panel)),
            "切回后的前台流不得因全局 req_id 递增而被丢弃",
        )


# ═══════════════════════════════════════════════════════════════════
#  (b) 守卫：未知 req_id 拒绝
# ═══════════════════════════════════════════════════════════════════


class TestUnknownReqIdRejection(unittest.TestCase):
    """无任何运行态流匹配的 req_id：不更新 DOM。"""

    def test_unknown_req_id_is_rejected(self):
        panel = _make_panel()
        panel._ai_running_convs = {"convA": _stream_state(req_id=STREAM_REQ_ID)}
        panel._ai_conversation_id = "convA"
        panel._ai_messages = _assistant_tool_call_messages()

        AIChatPanel._on_tool_result(panel, TOOL_CALL_ID, RESULT_TEXT, STATUS_OK, 99)

        self.assertEqual(_js_calls(panel), [], "未知 req_id 不得更新 DOM")

    def test_unknown_req_id_with_no_running_streams_is_rejected(self):
        panel = _make_panel()
        panel._ai_running_convs = {}
        panel._ai_conversation_id = "convA"
        panel._ai_messages = _assistant_tool_call_messages()

        AIChatPanel._on_tool_result(panel, TOOL_CALL_ID, RESULT_TEXT, STATUS_OK, 99)

        self.assertEqual(_js_calls(panel), [], "运行态为空时不得更新 DOM")


# ═══════════════════════════════════════════════════════════════════
#  (c) 守卫：背景会话拒绝
# ═══════════════════════════════════════════════════════════════════


class TestBackgroundConversationRejection(unittest.TestCase):
    """req_id 归属的流位于非当前可见会话：不更新 DOM。"""

    def test_background_stream_tool_result_is_rejected(self):
        panel = _make_panel()
        panel._ai_running_convs = {"convB": _stream_state(req_id=STREAM_REQ_ID)}
        panel._ai_conversation_id = "convA"  # 当前可见的是 A，流在 B
        panel._ai_messages = _assistant_tool_call_messages()

        AIChatPanel._on_tool_result(panel, TOOL_CALL_ID, RESULT_TEXT, STATUS_OK, STREAM_REQ_ID)

        self.assertEqual(
            _js_calls(panel), [],
            "背景会话（非当前可见）的工具结果不得更新可见 DOM",
        )


# ═══════════════════════════════════════════════════════════════════
#  (d) 守卫：非流式状态拒绝
# ═══════════════════════════════════════════════════════════════════


class TestNonStreamingStateRejection(unittest.TestCase):
    """可见会话的运行态流已非流式（streaming=False）：不更新 DOM。"""

    def test_non_streaming_running_state_is_rejected(self):
        panel = _make_panel()
        panel._ai_running_convs = {
            "convA": _stream_state(req_id=STREAM_REQ_ID, streaming=False)
        }
        panel._ai_conversation_id = "convA"
        panel._ai_messages = _assistant_tool_call_messages()

        AIChatPanel._on_tool_result(panel, TOOL_CALL_ID, RESULT_TEXT, STATUS_OK, STREAM_REQ_ID)

        self.assertEqual(
            _js_calls(panel), [],
            "非流式运行态不得接受工具结果更新 DOM",
        )


# ═══════════════════════════════════════════════════════════════════
#  (e) 基架校验：增量工具关闭时直接短路
# ═══════════════════════════════════════════════════════════════════


class TestIncrementalToolsGate(unittest.TestCase):
    """enable_incremental_tools=False 时 _on_tool_result 直接返回。"""

    def test_disabled_incremental_tools_short_circuits(self):
        panel = _make_panel()
        panel._ai_settings_store = SimpleNamespace(enable_incremental_tools=False)
        panel._ai_running_convs = {"convA": _stream_state(req_id=STREAM_REQ_ID)}
        panel._ai_conversation_id = "convA"
        panel._ai_messages = _assistant_tool_call_messages()

        AIChatPanel._on_tool_result(panel, TOOL_CALL_ID, RESULT_TEXT, STATUS_OK, STREAM_REQ_ID)

        self.assertEqual(_js_calls(panel), [], "增量工具关闭时不得更新 DOM")

    def test_missing_settings_store_short_circuits(self):
        panel = _make_panel()
        panel._ai_settings_store = None
        panel._ai_running_convs = {"convA": _stream_state(req_id=STREAM_REQ_ID)}
        panel._ai_conversation_id = "convA"
        panel._ai_messages = _assistant_tool_call_messages()

        AIChatPanel._on_tool_result(panel, TOOL_CALL_ID, RESULT_TEXT, STATUS_OK, STREAM_REQ_ID)

        self.assertEqual(_js_calls(panel), [], "settings store 缺失时不得更新 DOM")


# ═══════════════════════════════════════════════════════════════════
#  (f) 守卫：未找到对应 tool_call 时拒绝（_find_tool_call_by_id 真实实现）
# ═══════════════════════════════════════════════════════════════════


class TestUnknownToolCallRejection(unittest.TestCase):
    """归属校验通过但消息列表无匹配 tool_call → 不更新 DOM。"""

    def test_unmatched_tool_call_id_is_rejected(self):
        panel = _make_panel()
        panel._ai_running_convs = {"convA": _stream_state(req_id=STREAM_REQ_ID)}
        panel._ai_conversation_id = "convA"
        panel._ai_messages = _assistant_tool_call_messages()

        AIChatPanel._on_tool_result(panel, "call_no_such", RESULT_TEXT, STATUS_OK, STREAM_REQ_ID)

        self.assertEqual(_js_calls(panel), [], "未找到 tool_call 定义时不得更新 DOM")


if __name__ == "__main__":
    unittest.main()
