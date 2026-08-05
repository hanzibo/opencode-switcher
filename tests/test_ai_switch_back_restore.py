#!/usr/bin/env python3
"""AI 后台流式「切回（A→B→A）恢复」RED 回归测试（fix/ai-background-stream-persistence）。

已知 bug（本文件为纯测试，不修改生产代码）：
1. ``_switch_to_conversation`` 对**正在流式**的会话用 ``updateContent`` 原地重建
   ``#content``，却保留 ``_streaming_container_created`` 与
   ``st["response_div_added"]=True`` → 切回后 ``_render_current_assistant_message``
   不再发送 ``appendMessageContainer``，流式容器永远不会重建，后续
   ``updateMessageContainer`` 更新全部落到已被销毁的 DOM 节点上（更新丢失）。
2. ``_handle_stream_end(req_id)`` 用面板全局 ``_ai_request_id`` 判等；会话切换会
   递增 ``_ai_request_id`` → 切回后完成的旧 req_id 流被提前 return，
   ``_finalize_streaming_render`` 与 ``_save_current_conversation`` 被跳过，
   对话不持久化、``_ai_streaming`` 残留 True。

覆盖：
- (a) 含未解决 bash 工具调用/推理的流式会话，A→B→A 切回后下一次渲染 tick
  必须请求 ``appendMessageContainer('msg-<req_id>')`` 与
  ``updateMessageContainer('msg-<req_id>', ...)``（当前：缺 append → FAIL）。
- (b) ``_handle_stream_end(req_id)`` 在会话已切回/为当前会话时仍应保存，
  即使原始请求开始于会话切换之前（当前：全局 req_id 不匹配 → 提前 return → FAIL）。
- (c) 真正过期/非当前会话的流结束**不得**终结当前会话（守卫测试，当前通过）。
- (d) 睡眠式阻塞工具可用 ``threading.Event`` 表示；完成后完整消息序列
  包含 tool_call / tool_result / 最终 assistant 数据（基线测试，验证夹具语义）。

复用既有无头假面板模式：``AIChatPanel.__new__`` + 桩属性 + 假 WebView
（同 tests/test_system_prompt.py 与 tests/test_webview_reload_guard.py）。
"""
import os
import threading
import unittest
import json
from types import SimpleNamespace
from unittest import mock

os.environ.setdefault("GDK_BACKEND", "dummy")  # 无头环境导入 GTK

from system.event_types import (
    ToolCallData,
    reasoning_delta,
    stream_end,
    text_delta,
    tool_calls_event,
)
from views.ai_chat_panel import AIChatPanel


# ═══════════════════════════════════════════════════════════════════
#  无头假件（display-independent）
# ═══════════════════════════════════════════════════════════════════


class _FakeWebView:
    """记录 run_javascript 调用的假 WebView（同 test_webview_reload_guard）。"""

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
    """子代理条/分隔符等轻量控件的 no-op 替身。"""

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


class _FakeConversationStore:
    """内存会话存储：load_conversation 返回 SimpleNamespace 会话对象。"""

    def __init__(self, conversations):
        self._conversations = conversations  # conv_id -> dict(messages=[dict...], ...)

    def load_conversation(self, conv_id):
        spec = self._conversations.get(conv_id)
        if not spec:
            return None
        return SimpleNamespace(
            id=conv_id,
            title=spec.get("title", "New Conversation"),
            created_at=spec.get("created_at", 0),
            summary=spec.get("summary", ""),
            system_prompt=spec.get("system_prompt", ""),
            messages=[
                SimpleNamespace(
                    role=m.get("role"),
                    content=m.get("content"),
                    tool_call_id=m.get("tool_call_id"),
                    name=m.get("name"),
                    tool_calls=m.get("tool_calls"),
                    reasoning_content=m.get("reasoning_content"),
                )
                for m in spec["messages"]
            ],
        )

    def save_conversation(self, conv, bump_updated_at=True):
        pass


class _ScriptedLLM:
    """按调用次序返回预置事件流的假 LLM 客户端。"""

    def __init__(self, streams):
        self.streams = list(streams)
        self.call_count = 0

    def stream_chat_completion(self, *args, **kwargs):
        if self.call_count >= len(self.streams):
            return iter([])
        stream = self.streams[self.call_count]
        self.call_count += 1
        return iter(stream)

    def cancel_active_request(self, *args, **kwargs):
        pass


def _make_panel(**overrides):
    """无 GTK 的假 AIChatPanel：__new__ + 桩属性（test_system_prompt 模式）。

    实例级遮蔽 GTK 容器方法（无底层 GObject），并桩掉真实 IO / 与断言无关的
    重路径（save/render/finalize 等），使被测逻辑落在真实面板方法上。
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
    # 注意：_show_tool_details 是只读 property（读 _ai_settings_store.show_tool_details）
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


def _streaming_state(req_id=5):
    """正在流式的背景会话状态：含未解决的 bash 工具调用 + 推理文本。

    ``response_div_added=True`` 表示切走之前该容器已在 DOM 中渲染过——
    A→B→A 切回后它成为残留标记（DOM 已被 updateContent 重建）。
    """
    return {
        "streaming": True,
        "req_id": req_id,
        "request_key": ("ai", "convA", req_id),
        "cancel_event": threading.Event(),
        "messages": [
            {"role": "user", "content": "帮我跑一下测试"},
            {
                "role": "assistant",
                "content": "",
                "reasoning_content": "先分析一下需要执行的命令",
                "tool_calls": [
                    {"id": "call_bash_1", "type": "function",
                     "function": {"name": "bash",
                                  "arguments": "{\"command\": \"pytest tests/\"}"}},
                ],
            },
        ],
        "current_assistant_text": "",
        "current_reasoning_text": "先分析一下需要执行的命令",
        "response_div_added": True,  # 切回后成为陈旧标记（bug 1 的核心）
        "ai_markdown_text": "<p>部分渲染内容</p>",
    }


# ═══════════════════════════════════════════════════════════════════
#  (a) A→B→A 切回后流式 DOM 必须重新挂载
# ═══════════════════════════════════════════════════════════════════


class TestSwitchBackRebindsStreamingDom(unittest.TestCase):
    """bug 1：切回正在流式的会话后，陈旧 DOM 标记必须失效并重新 append 容器。"""

    def test_switch_back_requests_append_and_update_for_req_id(self):
        panel = _make_panel()
        st = _streaming_state()
        panel._ai_running_convs = {"convA": st}
        panel._ai_conversation_id = "convA"
        panel._ai_messages = st["messages"]
        panel._ai_request_id = 5
        panel._ai_streaming = True
        panel._streaming_container_created = True  # 切走前已建容器（陈旧）
        panel._ai_response_div_added = True
        panel._ai_html_cache = {
            "convA": "<div id='content'>A 的部分流式渲染</div>",
            "convB": "<div id='content'>B 的历史消息</div>",
        }
        panel._conversation_store = _FakeConversationStore({
            "convA": {"messages": st["messages"]},
            "convB": {"messages": [{"role": "user", "content": "B 的问题"}]},
        })

        # 用户切到 B，再切回正在流式的 A
        AIChatPanel._switch_to_conversation(panel, "convB")
        AIChatPanel._switch_to_conversation(panel, "convA")
        # 切回路径用 updateContent 重建了 #content（记录在案）
        self.assertIn("updateContent", panel._ai_webview.js_calls[-1])
        # 面板全局 req_id 随切换递增（为 (b) 提供上下文）
        self.assertEqual(panel._ai_request_id, 7)

        # 后台流的下一次渲染 tick
        AIChatPanel._render_current_assistant_message(panel, st["req_id"])

        js = "\n".join(panel._ai_webview.js_calls)
        # 容器被 updateContent 销毁 → 必须重新 appendMessageContainer
        self.assertIn(
            f"appendMessageContainer('msg-{st["req_id"]}')", js,
            "切回后未重新挂载流式消息容器（陈旧 response_div_added 导致 append 被跳过）",
        )
        # 并针对同一 req_id 的容器请求增量更新
        self.assertIn(
            f"updateMessageContainer('msg-{st["req_id"]}'", js,
            "切回后未对目标 req_id 请求 updateMessageContainer",
        )

    def test_switch_back_keeps_background_stream_messages_intact(self):
        """切走期间后台流消息不被污染；切回后面板消息即状态消息（同对象）。"""
        panel = _make_panel()
        st = _streaming_state()
        panel._ai_running_convs = {"convA": st}
        panel._ai_conversation_id = "convA"
        panel._ai_messages = st["messages"]
        panel._ai_request_id = 5
        panel._ai_streaming = True
        panel._ai_html_cache = {
            "convA": "<div id='content'>A</div>",
            "convB": "<div id='content'>B</div>",
        }
        panel._conversation_store = _FakeConversationStore({
            "convA": {"messages": st["messages"]},
            "convB": {"messages": [{"role": "user", "content": "B 的问题"}]},
        })

        AIChatPanel._switch_to_conversation(panel, "convB")
        AIChatPanel._switch_to_conversation(panel, "convA")

        self.assertIs(panel._ai_messages, st["messages"])
        self.assertTrue(panel._ai_streaming)
        self.assertEqual(
            [m["role"] for m in panel._ai_messages],
            ["user", "assistant"],
            "切回后消息历史应与流式状态一致",
        )

    def test_ensure_container_after_rebind_uses_stream_req_id(self):
        """切回后 _ensure_streaming_container 用流实例 req_id 建容器（非全局递增 id）。"""
        panel = _make_panel()
        st = _streaming_state(req_id=5)
        panel._ai_running_convs = {"convA": st}
        panel._ai_conversation_id = "convA"
        panel._ai_messages = st["messages"]
        panel._ai_request_id = 7  # A→B→A 已递增
        panel._ai_streaming = True
        panel._streaming_container_created = False

        # 切回路径：_rebind_active_stream 复位陈旧标记并调度渲染 tick（无主循环不触发）
        AIChatPanel._rebind_active_stream(panel, st)
        AIChatPanel._ensure_streaming_container(panel)

        js = "\n".join(panel._ai_webview.js_calls)
        self.assertIn(
            "appendMessageContainer('msg-5');", js,
            "切回后 token/推理 flush 的容器必须以流实例 req_id 命名（而非递增后的全局 id）",
        )
        self.assertNotIn(
            "msg-7", js,
            "不得用全局 _ai_request_id 建重复/错误的容器",
        )
        self.assertTrue(panel._streaming_container_created)

    def test_token_flush_after_rebind_targets_stream_container(self):
        """切回后 token flush 走 _ensure_streaming_container 并追加到流实例容器。"""
        panel = _make_panel()
        st = _streaming_state(req_id=5)
        panel._ai_running_convs = {"convA": st}
        panel._ai_conversation_id = "convA"
        panel._ai_messages = st["messages"]
        panel._ai_request_id = 7
        panel._ai_streaming = True
        panel._streaming_container_created = False
        panel._flush_scheduled = False
        panel._flush_source_id = 0
        panel._token_buffer = "增量 token"

        AIChatPanel._flush_token_buffer(panel)

        js = "\n".join(panel._ai_webview.js_calls)
        self.assertIn(
            "appendMessageContainer('msg-5');", js,
            "token flush 必须先以流实例 req_id 建立/复用容器",
        )
        self.assertIn(f"appendStreamToken({json.dumps('增量 token')});", js)
        self.assertNotIn("msg-7", js, "不得出现递增后全局 id 的容器")
        self.assertEqual(panel._token_buffer, "", "flush 后 buffer 应清空")



# ═══════════════════════════════════════════════════════════════════
#  (b)/(c) 流结束持久化：切回后仍保存；过期请求不终结错误会话
# ═══════════════════════════════════════════════════════════════════


class TestStreamEndPersistenceAfterRebind(unittest.TestCase):
    """bug 2：_handle_stream_end 以面板全局 req_id 判等，切回后旧流被跳过。"""

    def test_stream_end_saves_rebound_conversation(self):
        """切回后（req_id 已非全局当前值）完成的流仍须 finalize 并保存当前会话。"""
        panel = _make_panel()
        st = _streaming_state(req_id=5)
        panel._ai_running_convs = {"convA": st}
        panel._ai_conversation_id = "convA"
        panel._ai_messages = st["messages"]
        panel._ai_request_id = 7  # 期间 A→B→A，面板全局 req_id 已递增
        panel._ai_streaming = True

        AIChatPanel._handle_stream_end(panel, 5)

        panel._finalize_streaming_render.assert_called_once()
        panel._save_current_conversation.assert_called_once()
        self.assertFalse(
            panel._ai_streaming,
            "切回后完成的流必须清除 _ai_streaming（当前被提前 return 残留 True）",
        )

    def test_stale_stream_end_does_not_finalize_current_conversation(self):
        """守卫：过期/非当前会话的流结束不得终结当前会话（错误会话保存）。"""
        panel = _make_panel()
        st_b = _streaming_state(req_id=9)
        panel._ai_running_convs = {"convB": st_b}
        panel._ai_conversation_id = "convA"
        panel._ai_messages = [{"role": "user", "content": "A 当前正在对话"}]
        panel._ai_request_id = 10
        panel._ai_streaming = True

        AIChatPanel._handle_stream_end(panel, 9)

        panel._save_current_conversation.assert_not_called()
        panel._finalize_streaming_render.assert_not_called()
        self.assertTrue(panel._ai_streaming, "A 的流式状态不应被 B 的流结束终结")
        # 面板当前会话的 send 按钮状态不应被触碰
        self.assertEqual(panel._ai_send_btn.label, "")

    def test_finalize_after_rebind_targets_stream_req_id(self):
        """切回后流结束的最终渲染必须命中 msg-<流 req_id>（非全局递增 id）。"""
        panel = _make_panel()
        # 覆盖 mock：用真实 _finalize_streaming_render 捕获最终 JS 的 msg-id
        panel._finalize_streaming_render = AIChatPanel._finalize_streaming_render.__get__(panel, AIChatPanel)
        st = _streaming_state(req_id=5)
        panel._ai_running_convs = {"convA": st}
        panel._ai_conversation_id = "convA"
        panel._ai_messages = st["messages"]
        panel._ai_request_id = 7
        panel._ai_streaming = True
        panel._ai_html_cache = {}
        panel._last_rendered_html = ""
        panel._ai_markdown_text = st["ai_markdown_text"]
        panel._token_buffer = ""
        panel._reasoning_buffer = ""
        panel._flush_scheduled = False
        panel._flush_source_id = 0
        panel._reasoning_flush_scheduled = False
        panel._reasoning_flush_source_id = 0

        # 真实收尾链：状态先被弹出，再按 conv_id 收尾（模拟 _on_llm_api_finished）
        panel._ai_running_convs.pop("convA", None)
        AIChatPanel._handle_stream_end(panel, 5, "convA")

        js = "\n".join(panel._ai_webview.js_calls)
        self.assertIn(
            "updateMessageContainer('msg-5',", js,
            "最终渲染必须落到流实例 msg-5 容器（与流式渲染同一 id）",
        )
        self.assertNotIn(
            "updateMessageContainer('msg-7',", js,
            "不得用递增后的全局 id 渲染到错误/不存在的容器",
        )
        self.assertFalse(panel._ai_streaming)
        panel._save_current_conversation.assert_called_once()

    def test_finalize_flush_targets_stream_req_id_after_rebind(self):
        """切回后流结束的 token flush 必须以流实例 req_id 建容器（非全局递增 id）。

        bug：``_finalize_streaming_render`` 在流状态已从 ``_ai_running_convs``
        弹出后调用 ``_flush_token_buffer()``（未传 req_id），
        ``_active_stream_req_id`` 因此回退到全局 ``_ai_request_id`` → 创建
        ``msg-7`` 残留容器并把残余 token ``appendStreamToken`` 写入错误 id，
        而最终 ``updateMessageContainer('msg-5')`` 指向流实例容器（当前：msg-7
        残留 → FAIL）。空 buffer 时 ``_flush_token_buffer`` 被跳过，故必须用
        非空 ``_token_buffer`` 触发该路径。
        """
        panel = _make_panel()
        # 覆盖 mock：用真实 _finalize_streaming_render 捕获残余 token flush 的 msg-id
        panel._finalize_streaming_render = AIChatPanel._finalize_streaming_render.__get__(panel, AIChatPanel)
        st = _streaming_state(req_id=5)
        panel._ai_running_convs = {"convA": st}
        panel._ai_conversation_id = "convA"
        panel._ai_messages = st["messages"]
        panel._ai_request_id = 7  # 期间 A→B→A，面板全局 req_id 已递增
        panel._ai_streaming = True
        panel._streaming_container_created = False  # 容器未建 → flush 会触发 append
        panel._ai_html_cache = {}
        panel._last_rendered_html = ""
        panel._ai_markdown_text = st["ai_markdown_text"]
        panel._token_buffer = "残余 token"
        panel._reasoning_buffer = ""
        panel._flush_scheduled = False
        panel._flush_source_id = 0
        panel._reasoning_flush_scheduled = False
        panel._reasoning_flush_source_id = 0

        # 真实收尾链：状态先被弹出，再按 conv_id 收尾（模拟 _on_llm_api_finished）
        panel._ai_running_convs.pop("convA", None)
        AIChatPanel._handle_stream_end(panel, 5, "convA")

        js = "\n".join(panel._ai_webview.js_calls)
        self.assertIn(
            "appendMessageContainer('msg-5');", js,
            "finalize 的 token flush 必须用流实例 msg-5 建立容器（不得回退全局 id）",
        )
        self.assertIn(f"appendStreamToken({json.dumps('残余 token')});", js)
        self.assertNotIn(
            "msg-7", js,
            "不得用递增后的全局 id 创建残留容器/渲染错误 id",
        )
        self.assertIn(
            "updateMessageContainer('msg-5',", js,
            "最终渲染必须落到流实例 msg-5 容器",
        )
        self.assertEqual(panel._token_buffer, "", "finalize 后 token buffer 应清空")
        self.assertFalse(panel._ai_streaming)
        panel._save_current_conversation.assert_called_once()

    def test_non_streaming_flush_discards_buffer_without_append(self):
        """守卫：非流式状态下 flush 直接丢弃残留 buffer，不发送任何 append JS。

        当 ``_ai_streaming`` 已为 False（流已结束/收尾前置非流式），
        ``_flush_token_buffer`` 必须静默清空 buffer，不得再
        ``appendMessageContainer`` / ``appendStreamToken`` —— 否则会在流结束后
        重建残留容器并向 DOM 写入过期 token。
        """
        panel = _make_panel()
        panel._ai_streaming = False
        panel._streaming_container_created = False
        panel._token_buffer = "残留 token"
        panel._flush_scheduled = False
        panel._flush_source_id = 0

        AIChatPanel._flush_token_buffer(panel)

        self.assertEqual(panel._token_buffer, "", "非流式状态下 buffer 应被丢弃清空")
        self.assertFalse(panel._flush_scheduled)
        js = "\n".join(panel._ai_webview.js_calls)
        self.assertNotIn("appendMessageContainer", js, "非流式不得创建新容器")
        self.assertNotIn("appendStreamToken", js, "非流式不得向 JS 追加 token")

    def test_unknown_stream_end_without_visible_conversation_is_noop(self):
        """防御：归属无法解析且无可见会话的孤儿流结束不得执行收尾/保存。"""
        panel = _make_panel()
        panel._ai_running_convs = {}
        panel._ai_conversation_id = None
        panel._ai_request_id = 3
        panel._ai_messages = [{"role": "user", "content": "A"}]

        AIChatPanel._handle_stream_end(panel, 99)

        panel._save_current_conversation.assert_not_called()
        panel._finalize_streaming_render.assert_not_called()
        self.assertEqual(panel._ai_send_btn.label, "", "send 按钮不应被触碰")



# ═══════════════════════════════════════════════════════════════════
#  (d) 睡眠式阻塞工具 + 完整消息序列（基线/夹具验证）
# ═══════════════════════════════════════════════════════════════════


class TestBlockingToolCompletion(unittest.TestCase):
    """阻塞工具（bash 睡眠式）用 threading.Event 表示；完成后消息序列完整。

    走真实 run_llm_react_loop + 真实面板 _on_llm_api_finished/_handle_stream_end
    （req_id == 当前时），验证 (a) 依赖的夹具语义：状态消息在后台持续累积，
    最终包含 tool_call / tool_result / final assistant。
    """

    def test_blocking_tool_full_message_sequence(self):
        panel = _make_panel()
        panel._ai_conversation_id = "convA"
        panel._ai_request_id = 5  # 与 req_id 一致：完整走真实收尾链
        panel._ai_messages = [{"role": "user", "content": "跑一下测试"}]
        panel._ai_streaming = True
        panel._streaming_container_created = True
        panel._ai_response_div_added = True

        gate = threading.Event()   # 模拟睡眠式阻塞工具（bash 长命令）
        entered = threading.Event()

        def blocking_tool(tc, ctx):
            entered.set()
            gate.wait(5)
            return "✅ 命令执行完成"

        panel._llm_client = _ScriptedLLM([
            # 第一轮：推理 → 文本 → bash 工具调用
            [
                reasoning_delta("先确认环境"),
                text_delta("让我执行"),
                tool_calls_event([ToolCallData(
                    id="call_bash_1", name="bash",
                    arguments='{"command": "sleep 1 && echo done"}',
                )]),
                stream_end(),
            ],
            # 第二轮：纯文本收尾
            [text_delta("bash 已执行完成"), stream_end()],
        ])

        with mock.patch(
            "ai_engine.ai_tool_loop._execute_tool_call", side_effect=blocking_tool
        ), mock.patch(
            "ai_engine.ai_tool_loop._get_max_tool_iterations", return_value=25
        ), mock.patch(
            "ai_engine.ai_tool_loop.GLib.idle_add", side_effect=lambda fn, *a: fn(*a)
        ), mock.patch(
            "stores.skill_store.SkillStore.get_skills_prompt_summary", return_value=""
        ), mock.patch(
            "tool_registry.get_bash_cwd", return_value="/tmp"
        ), mock.patch(
            "tool_registry.check_background_subagents", return_value=None
        ):
            thread = threading.Thread(
                target=AIChatPanel._run_llm_api_request,
                args=(panel, "https://example.com/v1", "test-key", "deepseek-v4-flash",
                      [{"role": "user", "content": "跑一下测试"}], 5),
                kwargs={"conv_id": "convA"},
            )
            thread.start()
            self.assertTrue(entered.wait(5), "工具执行未进入阻塞态")

            # 阻塞期间：会话仍标记流式，已持有未解决的 tool_call 消息
            st = panel._ai_running_convs["convA"]
            self.assertTrue(st["streaming"])
            self.assertEqual(st["messages"][1]["role"], "assistant")
            self.assertEqual(
                st["messages"][1]["tool_calls"][0]["function"]["name"], "bash",
                "阻塞期间消息应已含未解决的 bash tool_call",
            )
            self.assertIs(panel._ai_messages, st["messages"], "当前会话消息应镜像后台状态")

            gate.set()
            thread.join(5)
            self.assertFalse(thread.is_alive(), "工具循环未在释放后完成")

        # 完成后：tool_call → tool_result → final assistant 全序列
        self.assertEqual(
            [m["role"] for m in st["messages"]],
            ["user", "assistant", "tool", "assistant"],
            "完整消息序列缺失（期望 tool_call/tool_result/final assistant）",
        )
        self.assertEqual(st["messages"][2]["tool_call_id"], "call_bash_1")
        self.assertIn("完成", st["messages"][2]["content"])
        self.assertIn("bash 已执行完成", st["messages"][3]["content"])
        self.assertFalse(st["streaming"])
        # 面板侧：真实 _handle_stream_end 链（req_id 当前）应保存会话
        panel._save_current_conversation.assert_called_once()


if __name__ == "__main__":
    unittest.main()
