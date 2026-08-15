#!/usr/bin/env python3
"""从历史下拉删除「当前激活会话」的回归测试（fix/ai-delete-active-conversation）。

T1（9134f2e）在 ``views/ai_chat_panel.py`` 修复了删除激活会话的三个缺陷：
- **幽灵条目**：删除激活会话后面板仍指向旧 id，``_get_sorted_conversations``
  经 active 分支/``_ai_running_convs`` 分支把旧 id 重新注入下拉列表。
- **磁盘复活**：删除后后台流/保存路径把旧 id 重新写回磁盘。
- **流式竞态**：``_delete_conversation_cleanup`` 不 pop ``_ai_running_convs``，
  流结束回调误收尾已删除会话。

修复手段（T1）：handler 在删除激活会话后调用 ``_reset_ai_panel_silent()``
（换新 id + 清空消息 + 重置 created_at，天然恰一次）；cleanup 负责 pop 运行态；
``_deleted_conversation_ids`` tombstone 集合短路后台渲染/流结束/保存。

本文件锁定上述行为（纯无头测试，不修改生产代码）：
- (1) 删除激活会话 → 面板复位（messages 清空 / id 换新 / created_at 重置）、
  store 删除一次、排序列表无旧 id。
- (2) 删除非激活会话 → 面板状态完全不变、不触发 reset。
- (3) 多删含激活 → reset 恰一次、全部文件删除、最终状态一致。
- (4) cleanup pop 运行态 → 已删除会话的流结束回调短路返回。
- (5) 删除激活会话后排序列表只含 store 剩余会话（无注入旧 id 的幽灵条目）。
- (6) tombstone 短路后台渲染/流结束的保存；新对话（非 tombstone）照常持久化。

复用既有无头假面板模式：``AIChatPanel.__new__`` + 桩属性 + 假会话存储
（同 tests/test_ai_switch_unsaved_running.py）。
"""
import os

os.environ.setdefault("GDK_BACKEND", "dummy")  # 无头环境导入 GTK

import threading
import time
import unittest
from types import SimpleNamespace
from unittest import mock

from views.ai_chat_panel import AIChatPanel
from gi.repository import WebKit2


# ═══════════════════════════════════════════════════════════════════
#  无头假件（display-independent）
# ═══════════════════════════════════════════════════════════════════


class _FakeConversationStore:
    """内存会话存储：delete 会真移除（list 反映）、记录 delete/save 调用。"""

    def __init__(self, convs=None):
        # convs: {conv_id: {"id","title","message_count","updated_at"}}
        self._convs = {cid: dict(meta) for cid, meta in (convs or {}).items()}
        self.deleted = []   # delete_conversation 调用序列
        self.saved = []     # save_conversation 的 conv 对象
        self.created = []   # create_conversation 的 kwargs

    def list_conversations(self):
        return [dict(meta) for meta in self._convs.values()]

    def load_conversation(self, conv_id):
        return self._convs.get(conv_id)

    def delete_conversation(self, conv_id):
        self.deleted.append(conv_id)
        self._convs.pop(conv_id, None)

    def save_conversation(self, conv, bump_updated_at=True):
        self.saved.append(conv)

    def create_conversation(self, **kwargs):
        self.created.append(kwargs)
        return SimpleNamespace(id=f"new_{len(self.created)}")


class _FakeDecision:
    """NAVIGATION_ACTION 决策桩：仅暴露 handler 所需的 get_navigation_action/ignore。"""

    def __init__(self, uri):
        self._uri = uri
        self.ignore_called = False

    def get_navigation_action(self):
        return SimpleNamespace(
            get_request=lambda: SimpleNamespace(get_uri=lambda: self._uri)
        )

    def ignore(self):
        self.ignore_called = True


class _FakeWebView:
    def run_javascript(self, js, *args):
        pass


class _FakeButton:
    def __init__(self):
        self.label = ""
        self.sensitive = True

    def set_label(self, label):
        self.label = label

    def set_sensitive(self, sensitive):
        self.sensitive = sensitive


class _FakeBuffer:
    def __init__(self):
        self.text = ""

    def set_text(self, text):
        self.text = text


class _FakeEntry:
    def __init__(self):
        self.placeholder_text = ""
        self.buffer = _FakeBuffer()

    def get_buffer(self):
        return self.buffer

    def grab_focus(self):
        pass


class _FakeSpinner:
    def stop(self):
        pass

    def hide(self):
        pass


class _FakeLabel:
    def set_markup(self, markup):
        pass


class _FakeWidget:
    def set_no_show_all(self, value):
        pass

    def show_all(self):
        pass

    def hide(self):
        pass


_NAV = WebKit2.PolicyDecisionType.NAVIGATION_ACTION


def _make_panel(store=None, **overrides):
    """无 GTK 的假 AIChatPanel：__new__ + 桩属性（test_system_prompt 模式）。

    实例级遮蔽 GTK 容器方法（无底层 GObject），并桩掉真实 IO / 与断言无关的
    重路径（render/finalize/title 生成等），使被测逻辑落在真实的面板方法上
    （``_on_decide_policy`` / ``_delete_conversation_cleanup`` /
    ``_get_sorted_conversations`` / ``_save_current_conversation`` /
    ``_on_llm_api_finished``）。
    """
    panel = AIChatPanel.__new__(AIChatPanel)
    panel._conversation_store = store if store is not None else _FakeConversationStore({})
    panel._ai_webview = _FakeWebView()
    panel._ai_running_convs = {}
    panel._deleted_conversation_ids = set()
    panel._ai_request_id = 0
    panel._ai_conversation_id = None
    panel._ai_conversation_created_at = 0
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
    panel._ai_pending_title_notification = False
    panel._ai_active_model_info = None
    panel._ai_last_prompt_obj = None
    panel._ai_cancelling = False
    panel._ai_render_timeout_id = 0
    panel._ai_assistant_buffer = ""
    panel._ai_assistant_html_base = ""
    panel._ai_error_pending_conv = None
    panel._ai_cancel_watchdog_id = 0
    panel._ai_send_btn = _FakeButton()
    panel._ai_entry = _FakeEntry()
    panel._ai_spinner = _FakeSpinner()
    panel._ai_lbl = _FakeLabel()
    panel._ai_history_popover = SimpleNamespace(
        refresh_dropdown=lambda: None,
        get_visible=lambda: False,
        popdown=lambda: None,
    )
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
        enable_answer_notification=False,
    )
    panel._llm_client = mock.Mock()
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
    panel._update_send_button = mock.Mock()
    panel._update_token_display = mock.Mock()
    panel._prune_messages = mock.Mock()
    panel._get_title_model_config = mock.Mock(return_value=None)
    panel._finalize_streaming_render = mock.Mock()
    panel._render_background_conversation = mock.Mock()
    panel._notify_ai_answer_finished = mock.Mock()
    panel._snapshot_system_prompt = lambda: None
    panel._clear_subagent_bar_instantly = lambda: None
    panel._refresh_subagent_bar = lambda: None
    panel._load_webview_html = lambda *a, **k: None
    for key, value in overrides.items():
        setattr(panel, key, value)
    return panel


def _conv_meta(conv_id, updated_at=1000):
    return {"id": conv_id, "title": conv_id, "message_count": 1, "updated_at": updated_at}


# ═══════════════════════════════════════════════════════════════════
#  回归测试：删除激活会话
# ═══════════════════════════════════════════════════════════════════


class TestDeleteActiveConversation(unittest.TestCase):
    """T1 修复行为：删除激活会话 → 面板受控复位 + 无幽灵条目 + 无复活。"""

    # ── (1) 删除激活会话 → 面板复位 ──
    def test_delete_active_resets_panel(self):
        """history-delete 删除激活会话：清空消息、换新 id、重置 created_at、
        store 删除一次、排序列表无旧 id。"""
        store = _FakeConversationStore({"convA": _conv_meta("convA")})
        panel = _make_panel(store=store)
        panel._ai_conversation_id = "convA"
        panel._ai_messages = [{"role": "user", "content": "激活会话内容"}]
        panel._ai_conversation_created_at = 12345
        # 计数包裹：仍走真实 reset 逻辑（换 id / 清空），同时可断言恰一次
        panel._reset_ai_panel_silent = mock.Mock(wraps=panel._reset_ai_panel_silent)

        panel._on_decide_policy(None, _FakeDecision("opencode://history-delete?id=convA"), _NAV)

        self.assertEqual(panel._ai_messages, [])
        self.assertNotEqual(panel._ai_conversation_id, "convA")
        self.assertGreater(panel._ai_conversation_created_at, 1e11)  # 毫秒时间戳已重置
        self.assertEqual(store.deleted, ["convA"])
        panel._reset_ai_panel_silent.assert_called_once()
        ids = [s["id"] for s in panel._get_sorted_conversations()]
        self.assertNotIn("convA", ids)

    # ── (2) 删除非激活会话 → 面板不动 ──
    def test_delete_non_active_leaves_panel_untouched(self):
        """history-delete 删除非激活会话：面板状态（id/messages/created_at）完全
        不变，``_reset_ai_panel_silent`` 不被调用。"""
        store = _FakeConversationStore({
            "convA": _conv_meta("convA", updated_at=2000),
            "convB": _conv_meta("convB", updated_at=1000),
        })
        panel = _make_panel(store=store)
        panel._ai_conversation_id = "convA"
        messages = [{"role": "user", "content": "激活会话内容"}]
        panel._ai_messages = messages
        panel._ai_conversation_created_at = 999
        panel._reset_ai_panel_silent = mock.Mock()

        panel._on_decide_policy(None, _FakeDecision("opencode://history-delete?id=convB"), _NAV)

        self.assertEqual(panel._ai_conversation_id, "convA")
        self.assertEqual(panel._ai_messages, messages)
        self.assertEqual(panel._ai_conversation_created_at, 999)
        panel._reset_ai_panel_silent.assert_not_called()
        self.assertEqual(store.deleted, ["convB"])

    # ── (3) 多删含激活 → reset 恰一次 ──
    def test_multi_delete_with_active_resets_once(self):
        """history-delete-multi 批量删除含激活会话：reset 恰一次（换新 id 后
        无后续匹配）、全部文件删除、最终面板一致（新 id / 空消息）。"""
        store = _FakeConversationStore({
            "convA": _conv_meta("convA", updated_at=3000),
            "convB": _conv_meta("convB", updated_at=2000),
            "convC": _conv_meta("convC", updated_at=1000),
        })
        panel = _make_panel(store=store)
        panel._ai_conversation_id = "convA"
        panel._ai_messages = [{"role": "user", "content": "激活会话内容"}]
        panel._ai_conversation_created_at = 111
        panel._reset_ai_panel_silent = mock.Mock(wraps=panel._reset_ai_panel_silent)

        panel._on_decide_policy(
            None, _FakeDecision("opencode://history-delete-multi?ids=convA,convB,convC"), _NAV
        )

        self.assertEqual(store.deleted, ["convA", "convB", "convC"])
        panel._reset_ai_panel_silent.assert_called_once()
        self.assertNotEqual(panel._ai_conversation_id, "convA")
        self.assertEqual(panel._ai_messages, [])
        self.assertIn("convA", panel._deleted_conversation_ids)

    # ── (4) cleanup pop 运行态 → 流结束回调短路 ──
    def test_cleanup_pops_running_state(self):
        """``_delete_conversation_cleanup`` 弹出 ``_ai_running_convs``；此后
        ``_on_llm_api_finished(req_id)`` 反查失败且回退 state 为 None → 提前返回，
        不渲染、不收尾、不保存。"""
        store = _FakeConversationStore({})
        panel = _make_panel(store=store)
        req_id = 7
        state = {
            "streaming": True,
            "req_id": req_id,
            "request_key": ("ai", "convA", req_id),
            "cancel_event": threading.Event(),
            "messages": [{"role": "user", "content": "流式中的问题"}],
            "current_assistant_text": "部分输出",
            "current_reasoning_text": "",
            "response_div_added": True,
            "ai_markdown_text": "",
        }
        panel._ai_running_convs = {"convA": state}
        panel._ai_conversation_id = "convA"  # 删除激活会话（reset 前的竞态窗口）
        panel._ai_messages = [{"role": "user", "content": "流式中的问题"}]
        panel._ai_cancelling = False
        panel._handle_stream_end = mock.Mock()

        panel._delete_conversation_cleanup("convA")

        self.assertNotIn("convA", panel._ai_running_convs)
        self.assertIn("convA", panel._deleted_conversation_ids)

        # 已删除会话的流结束回调必须短路返回
        panel._on_llm_api_finished(req_id)
        panel._finalize_streaming_render.assert_not_called()
        panel._render_background_conversation.assert_not_called()
        panel._handle_stream_end.assert_not_called()
        self.assertEqual(store.saved, [])

    # ── (5) 无幽灵条目：排序列表只含 store 剩余会话 ──
    def test_ghost_entry_removed_from_dropdown(self):
        """删除激活会话后 ``_get_sorted_conversations`` 不得经 active 分支或
        ``_ai_running_convs`` 分支把旧 id 重新注入下拉列表。"""
        store = _FakeConversationStore({
            "convA": _conv_meta("convA", updated_at=2000),
            "convB": _conv_meta("convB", updated_at=1000),
        })
        panel = _make_panel(store=store)
        panel._ai_conversation_id = "convA"
        panel._ai_messages = [{"role": "user", "content": "激活会话内容"}]
        # 删除前 convA 仍在运行（fix 前 cleanup 不 pop → 幽灵条目来源之一）
        panel._ai_running_convs = {
            "convA": {
                "streaming": True,
                "req_id": 3,
                "request_key": ("ai", "convA", 3),
                "cancel_event": threading.Event(),
                "messages": [{"role": "assistant", "content": "运行中的输出"}],
                "current_assistant_text": "",
                "current_reasoning_text": "",
                "response_div_added": True,
                "ai_markdown_text": "",
            }
        }
        panel._ai_conversation_created_at = 555

        panel._on_decide_policy(None, _FakeDecision("opencode://history-delete?id=convA"), _NAV)

        ids = [s["id"] for s in panel._get_sorted_conversations()]
        self.assertNotIn("convA", ids)
        self.assertEqual(ids, ["convB"])
        self.assertNotIn("convA", panel._ai_running_convs)

    # ── (6) tombstone 短路保存，新对话不受影响 ──
    def test_tombstone_blocks_save(self):
        """``_deleted_conversation_ids`` 含某 id 时，后台渲染/流结束/保存对其
        短路（不写磁盘）；当前新 id（非 tombstone）仍照常持久化。"""
        store = _FakeConversationStore({})
        panel = _make_panel(store=store)
        panel._deleted_conversation_ids = {"convA"}
        panel._ai_conversation_id = "convB"  # 删除后新建的当前会话
        panel._ai_messages = [{"role": "user", "content": "删除后的新问题"}]
        panel._ai_conversation_created_at = int(time.time() * 1000)
        panel._ai_system_prompt = ""
        panel._ai_summary = ""
        panel._ai_summary_generating = False
        panel._last_rendered_html = ""
        # 恢复真实 _save_current_conversation / _render_background_conversation
        #（_make_panel 默认 stub 掉保存/渲染重路径）
        panel._save_current_conversation = (
            AIChatPanel._save_current_conversation.__get__(panel, AIChatPanel)
        )
        panel._render_background_conversation = (
            AIChatPanel._render_background_conversation.__get__(panel, AIChatPanel)
        )

        # (a) 后台渲染对 tombstone id 短路（不写磁盘）
        panel._render_background_conversation(
            "convA", [{"role": "user", "content": "陈旧流"}], {"system_prompt": ""}
        )
        self.assertEqual(store.saved, [])
        # (b) 流结束对 tombstone id 短路（不 finalize、不保存）
        panel._handle_stream_end(1, "convA")
        panel._finalize_streaming_render.assert_not_called()
        self.assertEqual(store.saved, [])
        # (c) 当前新会话（非 tombstone）照常保存
        panel._save_current_conversation({"alias": "Default"})
        self.assertEqual(len(store.saved), 1)
        self.assertEqual(store.saved[0].id, "convB")


if __name__ == "__main__":
    unittest.main()
