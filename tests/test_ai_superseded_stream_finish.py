#!/usr/bin/env python3
"""同会话内被取代（superseded）流的完成回调不得终结/持久化新流。

安全审查结论（HIGH 本地数据完整性）：/retry 会弹掉当前会话的旧流状态并启动更新
``req_id`` 的新流；随后旧流的 ``_on_llm_api_finished(old_req_id)`` /
``_finalize_after_tool_loop(old_req_id)`` 反查 ``_ai_running_convs`` 失败，回退到
当前会话。若回退时不校验 ``req_id``，旧完成会读取**新流**的文本/消息、把
``streaming`` 标记为 False、弹掉新流状态并 ``_save_current_conversation`` 把新流的
部分输出提前落盘——新流随后变成孤儿、输出丢失、半成品状态被持久化。

修复：反查失败回退时**取回**当前会话的运行态，校验 ``state.get("req_id") ==
req_id``；被取代的旧完成（req_id 不同）或状态已弹掉（state is None）一律返回，
不触碰新流（不标记 streaming=False、不追加 assistant 文本、不渲染、不保存）。

覆盖：
- (a) 同会话 old req_id/new req_id：旧完成回调（``_on_llm_api_finished``）返回，
  新流状态/持久化保持原样（核心回归，当前未加守卫 → FAIL）。
- (b) 同会话 old req_id/new req_id：``_finalize_after_tool_loop`` 同款守卫。
- (c) 守卫：A→B→A 切回后合法的旧流（反查成功且 req_id 匹配）仍须 finalize+保存
  （当前通过，修复不得破坏切回语义）。
- (d) 守卫：反查失败且当前会话已无任何运行态流时，完成回调为 no-op（不追加/
  不保存/不改流式标记）。
- (e) 守卫：背景会话（反查成功、非当前会话）的完成仍走背景渲染+落盘（不被误伤）。

复用既有无头假面板模式：``AIChatPanel.__new__`` + 桩属性 + 假 WebView
（同 tests/test_ai_switch_back_restore.py）。
"""
import os
import threading
import unittest
from types import SimpleNamespace
from unittest import mock

os.environ.setdefault("GDK_BACKEND", "dummy")  # 无头环境导入 GTK

from views.ai_chat_panel import AIChatPanel


# ═══════════════════════════════════════════════════════════════════
#  无头假件（display-independent，同 test_ai_switch_back_restore）
# ═══════════════════════════════════════════════════════════════════


class _FakeWebView:
    def __init__(self):
        self.js_calls = []

    def run_javascript(self, js, *args):
        self.js_calls.append(js)


class _FakeButton:
    def __init__(self):
        self.label = ""
        self.sensitive = True

    def set_label(self, label):
        self.label = label

    def set_sensitive(self, sensitive):
        self.sensitive = sensitive


class _FakeSpinner:
    def start(self):
        pass

    def stop(self):
        pass

    def show(self):
        pass

    def hide(self):
        pass


class _FakeTextBuffer:
    def set_text(self, text):
        pass


class _FakeEntry:
    def __init__(self):
        self.placeholder_text = ""

    def get_buffer(self):
        return _FakeTextBuffer()

    def grab_focus(self):
        pass


class _FakeLabel:
    def set_markup(self, markup):
        pass


class _FakeWidget:
    def show(self):
        pass

    def show_all(self):
        pass

    def hide(self):
        pass

    def set_no_show_all(self, value):
        pass

    def get_style_context(self):
        return SimpleNamespace(remove_class=lambda c: None, add_class=lambda c: None)

    def get_children(self):
        return []

    def remove(self, child):
        pass


def _make_panel(**overrides):
    """无 GTK 的假 AIChatPanel：__new__ + 桩属性（test_system_prompt 模式）。

    桩掉真实 IO / 与断言无关的重路径（save/render/finalize 等），使被测逻辑落在
    真实的 ``_on_llm_api_finished``/``_finalize_after_tool_loop`` 上。
    """
    panel = AIChatPanel.__new__(AIChatPanel)
    panel._ai_webview = _FakeWebView()
    panel._ai_running_convs = {}
    panel._ai_request_id = 0
    panel._ai_conversation_id = None
    panel._ai_messages = []
    panel._ai_html_cache = {}
    panel._ai_markdown_text = ""
    panel._last_rendered_html = ""
    panel._ai_current_assistant_text = ""
    panel._ai_current_reasoning_text = ""
    panel._ai_response_div_added = False
    panel._ai_streaming = False
    panel._streaming_container_created = False
    panel._ai_system_prompt = ""
    panel._ai_summary = ""
    panel._ai_summary_generating = False
    panel._ai_title_generated = True  # 跳过标题生成线程
    panel._ai_active_model_info = None
    panel._ai_last_prompt_obj = None
    panel._ai_cancelling = False
    panel._ai_render_timeout_id = 0
    panel._flush_source_id = 0
    panel._flush_scheduled = False
    panel._reasoning_flush_source_id = 0
    panel._reasoning_flush_scheduled = False
    panel._ai_send_btn = _FakeButton()
    panel._ai_entry = _FakeEntry()
    panel._ai_spinner = _FakeSpinner()
    panel._ai_lbl = _FakeLabel()
    panel._ai_history_popover = SimpleNamespace(refresh_dropdown=lambda: None)
    panel._ai_subagent_bar = _FakeWidget()
    panel._ai_subagent_blocks = {}
    panel._ai_selected_subagents = set()
    panel.separator = _FakeWidget()
    panel._ai_input_area = _FakeWidget()
    panel._ai_settings_store = SimpleNamespace(
        soft_limit=500,
        trim_target=50,
        enable_summary=False,
        summary_threshold=3,
        enable_incremental_tools=False,
        disabled_tools=[],
        show_tool_details=True,
    )
    # GTK 容器方法：实例级遮蔽为 no-op（__new__ 对象无底层 GObject）
    panel.show = lambda: None
    panel.show_all = lambda: None
    panel.set_no_show_all = lambda v: None
    panel.queue_resize = lambda: None
    # 桩掉真实 IO / 重路径（与各测试断言正交）
    panel._read_model_config = lambda *a, **k: (
        "https://example.com/v1", "test-key", "deepseek-v4-flash", "Test Alias",
        0.3, 4096, 1.0, False, "high",
    )
    panel._build_model_snapshot = lambda: {"alias": "Default"}
    panel._save_current_conversation = mock.Mock()
    panel._update_token_display = mock.Mock()
    panel._prune_messages = mock.Mock()
    panel._get_title_model_config = mock.Mock(return_value=None)
    panel._finalize_streaming_render = mock.Mock()
    panel._on_tool_result = mock.Mock()
    panel._on_token_delta = mock.Mock()
    panel._on_reasoning_delta = mock.Mock()
    for key, value in overrides.items():
        setattr(panel, key, value)
    return panel


def _new_stream_state(req_id, messages=None):
    """同会话内新流（/retry 启动）的运行态。"""
    return {
        "streaming": True,
        "req_id": req_id,
        "request_key": ("ai", "convA", req_id),
        "cancel_event": threading.Event(),
        "messages": messages if messages is not None else [{"role": "user", "content": "重试后的问题"}],
        "current_assistant_text": "新流正在输出的内容",
        "current_reasoning_text": "",
        "response_div_added": False,
        "ai_markdown_text": "",
    }


# ═══════════════════════════════════════════════════════════════════
#  (a) 核心回归：同会话旧完成回调不得终结/持久化新流（_on_llm_api_finished）
# ═══════════════════════════════════════════════════════════════════


class TestSupersededLlmaFinish(unittest.TestCase):
    """/retry 弹掉旧状态后，旧 _on_llm_api_finished(old_req_id) 必须返回。"""

    def _panel_with_newer_stream(self, cancelling=True):
        """同会话 convA：旧流已被 /retry 弹出，新流（req_id=6）接管运行态。

        镜像 /retry 后的面板 UI：流式按钮 + 「等待回复中...」占位符（/retry 设置）。
        """
        panel = _make_panel()
        new_st = _new_stream_state(req_id=6)
        panel._ai_running_convs = {"convA": new_st}
        panel._ai_conversation_id = "convA"
        panel._ai_messages = new_st["messages"]
        panel._ai_current_assistant_text = "新流正在输出的内容"
        panel._ai_request_id = 6
        panel._ai_streaming = True
        panel._ai_cancelling = cancelling  # /retry 前可能按过暂停
        panel._ai_entry.placeholder_text = "等待回复中..."
        return panel, new_st

    def test_stale_finish_does_not_finalize_newer_stream_same_conversation(self):
        """旧完成（req_id=5）落在新流（req_id=6）上 → 新流状态/持久化保持原样。"""
        panel, new_st = self._panel_with_newer_stream()

        AIChatPanel._on_llm_api_finished(panel, 5)

        # 新流仍在运行：运行态未被弹掉/标记终止
        self.assertIn("convA", panel._ai_running_convs)
        self.assertTrue(panel._ai_running_convs["convA"]["streaming"])
        self.assertEqual(panel._ai_running_convs["convA"]["req_id"], 6)
        self.assertIs(panel._ai_running_convs["convA"], new_st, "运行态对象不得被替换")
        # 新流消息未被旧完成追加 assistant 文本
        self.assertEqual(panel._ai_messages, [{"role": "user", "content": "重试后的问题"}])
        self.assertNotIn(
            {"role": "assistant", "content": "新流正在输出的内容"},
            new_st["messages"],
            "旧完成不得把新流文本作为已完成 assistant 消息追加",
        )
        # 新流的流式标记/UI 状态不受影响
        self.assertTrue(panel._ai_streaming, "新流的流式状态不得被旧完成清除")
        self.assertEqual(panel._ai_send_btn.label, "")
        self.assertEqual(
            panel._ai_entry.placeholder_text, "等待回复中...",
            "旧完成不得复位新流的「等待回复中...」占位符",
        )
        # 无渲染、无保存、无收尾
        panel._finalize_streaming_render.assert_not_called()
        panel._save_current_conversation.assert_not_called()

    def test_stale_finish_does_not_reset_cancelling_or_entry(self):
        """旧完成返回后不得篡改新流所属的面板取消/输入框状态。"""
        panel, _ = self._panel_with_newer_stream(cancelling=True)

        AIChatPanel._on_llm_api_finished(panel, 5)

        self.assertEqual(panel._ai_entry.placeholder_text, "等待回复中...")
        self.assertTrue(panel._ai_streaming)


# ═══════════════════════════════════════════════════════════════════
#  (b) 核心回归：_finalize_after_tool_loop 同款守卫
# ═══════════════════════════════════════════════════════════════════


class TestSupersededToolLoopFinalize(unittest.TestCase):
    """/retry 弹掉旧状态后，旧 _finalize_after_tool_loop(old_req_id) 必须返回。"""

    def test_stale_finalize_tool_loop_does_not_touch_newer_stream(self):
        panel = _make_panel()
        new_st = _new_stream_state(req_id=6)
        panel._ai_running_convs = {"convA": new_st}
        panel._ai_conversation_id = "convA"
        panel._ai_messages = new_st["messages"]
        panel._ai_streaming = True
        panel._ai_cancelling = True
        panel._ai_entry.placeholder_text = "等待回复中..."

        AIChatPanel._finalize_after_tool_loop(panel, 5)

        # 新流状态未被终结
        self.assertIn("convA", panel._ai_running_convs)
        self.assertTrue(panel._ai_running_convs["convA"]["streaming"])
        self.assertEqual(panel._ai_running_convs["convA"]["req_id"], 6)
        self.assertTrue(panel._ai_streaming, "新流的流式状态不得被旧 finalize 清除")
        self.assertEqual(panel._ai_entry.placeholder_text, "等待回复中...")
        panel._finalize_streaming_render.assert_not_called()
        panel._save_current_conversation.assert_not_called()


# ═══════════════════════════════════════════════════════════════════
#  (c) 守卫：合法的切回（A→B→A）旧流仍须 finalize + 保存
# ═══════════════════════════════════════════════════════════════════


class TestValidReboundStreamPreserved(unittest.TestCase):
    """反查成功且 req_id 匹配的流（切回后旧流）不得被新守卫误杀。"""

    def test_rebound_stream_still_finalizes_and_saves(self):
        panel = _make_panel()
        st = _new_stream_state(req_id=5)
        panel._ai_running_convs = {"convA": st}
        panel._ai_conversation_id = "convA"
        panel._ai_messages = st["messages"]
        panel._ai_request_id = 7  # 期间 A→B→A，全局 req_id 已递增
        panel._ai_streaming = True

        AIChatPanel._on_llm_api_finished(panel, 5)

        # 收尾链完整：finalize 渲染 + 保存当前会话 + 清流式标记 + 弹出状态
        panel._finalize_streaming_render.assert_called_once()
        panel._save_current_conversation.assert_called_once()
        self.assertFalse(panel._ai_streaming)
        self.assertNotIn("convA", panel._ai_running_convs)

    def test_rebound_tool_loop_finalize_still_saves(self):
        panel = _make_panel()
        st = _new_stream_state(req_id=5)
        panel._ai_running_convs = {"convA": st}
        panel._ai_conversation_id = "convA"
        panel._ai_messages = st["messages"]
        panel._ai_request_id = 7
        panel._ai_streaming = True

        AIChatPanel._finalize_after_tool_loop(panel, 5)

        panel._finalize_streaming_render.assert_called_once()
        panel._save_current_conversation.assert_called_once()
        self.assertFalse(panel._ai_streaming)
        self.assertNotIn("convA", panel._ai_running_convs)


# ═══════════════════════════════════════════════════════════════════
#  (d) 守卫：反查失败且当前会话无任何运行态流 → no-op
# ═══════════════════════════════════════════════════════════════════


class TestStaleFinishNoRunningState(unittest.TestCase):
    """状态已被弹掉（无新流接管）的陈旧完成：不得追加/保存/改流式标记。"""

    def test_llm_finish_with_no_running_state_is_noop(self):
        panel = _make_panel()
        panel._ai_running_convs = {}  # 当前会话无运行态流
        panel._ai_conversation_id = "convA"
        panel._ai_messages = [{"role": "user", "content": "A 的消息"}]
        panel._ai_current_assistant_text = "不得被追加的陈旧文本"
        panel._ai_streaming = True
        panel._ai_cancelling = True  # 旧守卫曾靠该旗标放行 → 数据损坏；现在必须返回

        AIChatPanel._on_llm_api_finished(panel, 99)

        self.assertEqual(len(panel._ai_messages), 1, "陈旧完成不得追加 assistant 消息")
        self.assertEqual(panel._ai_messages[0], {"role": "user", "content": "A 的消息"})
        self.assertTrue(panel._ai_streaming, "陈旧完成不得改写面板流式标记")
        panel._save_current_conversation.assert_not_called()
        panel._finalize_streaming_render.assert_not_called()

    def test_tool_loop_finalize_with_no_running_state_is_noop(self):
        panel = _make_panel()
        panel._ai_running_convs = {}
        panel._ai_conversation_id = "convA"
        panel._ai_messages = [{"role": "user", "content": "A 的消息"}]
        panel._ai_streaming = True
        panel._ai_cancelling = True

        AIChatPanel._finalize_after_tool_loop(panel, 99)

        self.assertTrue(panel._ai_streaming)
        panel._save_current_conversation.assert_not_called()
        panel._finalize_streaming_render.assert_not_called()


# ═══════════════════════════════════════════════════════════════════
#  (e) 守卫：背景会话（反查成功、非当前）的完成不受影响
# ═══════════════════════════════════════════════════════════════════


class TestValidBackgroundStreamPreserved(unittest.TestCase):
    """切走后后台完成的流仍走背景渲染+落盘（反查成功路径不受守卫影响）。"""

    def test_background_finish_still_renders_and_pops(self):
        panel = _make_panel()
        st = _new_stream_state(req_id=5)
        panel._ai_running_convs = {"convB": st}
        panel._ai_conversation_id = "convA"  # 当前可见的是 A
        panel._ai_messages = [{"role": "user", "content": "A 的消息"}]
        panel._ai_request_id = 8
        panel._ai_streaming = True
        panel._render_background_conversation = mock.Mock()

        AIChatPanel._on_llm_api_finished(panel, 5)

        panel._render_background_conversation.assert_called_once_with(
            "convB", st["messages"], st
        )
        self.assertNotIn("convB", panel._ai_running_convs)
        self.assertFalse(st["streaming"])
        # 当前可见会话 A 不受影响
        self.assertTrue(panel._ai_streaming, "背景流结束不得终结当前可见会话")


if __name__ == "__main__":
    unittest.main()
