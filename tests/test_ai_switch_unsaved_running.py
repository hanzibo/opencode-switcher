#!/usr/bin/env python3
"""AI 切换「未落盘的正在流式会话」RED 回归测试（fix/ai-background-stream-persistence）。

已知 bug（本文件为纯测试，不修改生产代码）：
``_switch_to_conversation`` 先 ``load_conversation(conv_id)`` 再判断
``if not conv: return``——对**新建后尚未持久化**（磁盘上不存在）的正在流式
会话，load 返回 None → 提前 return，完全跳过 ``_ai_running_convs`` 的流式状态
恢复：面板既不切到目标会话，也不恢复消息/流式标记，后台流「凭空消失」。

覆盖：
- (a) 直接 ``_switch_to_conversation`` 切换到未落盘的流式会话：必须恢复
  ``_ai_conversation_id``/``_ai_messages``/``_ai_streaming``，复位陈旧 DOM 标记
  并重建流式容器（当前：提前 return → 状态不恢复 → FAIL）。
- (b) ``navigate_conversation`` 键盘路径：排序列表已含未落盘流式会话（见 (e)），
  键盘导航到它时必须真正切换（当前：``_switch_to_conversation`` 提前 return →
  面板停留在原会话 → FAIL）。
- (c) 目标既不在磁盘也不在运行态（非流式缺失目标）时切换必须为 no-op：
  不改当前会话、不渲染任何 JS（守卫测试，当前通过，修复后仍须通过）。
- (d) 已落盘且正在流式的会话切换后 ``_ai_conversation_created_at`` 必须保留
  磁盘 ``created_at``（守卫测试，当前通过——修复不得破坏磁盘路径语义）。
- (e) ``_get_sorted_conversations`` 必须包含未落盘的流式会话
  （``navigate_conversation`` 能定位目标的前提，锚定测试，当前通过）。

复用既有无头假面板模式：``AIChatPanel.__new__`` + 桩属性 + 假 WebView
（同 tests/test_ai_switch_back_restore.py）。
"""
import os
import threading
import unittest
from types import SimpleNamespace
from unittest import mock

os.environ.setdefault("GDK_BACKEND", "dummy")  # 无头环境导入 GTK

from views.ai_chat_panel import AIChatPanel, AI_BTN_LABEL_STOP


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
    """记录 start/stop/show/hide 状态的假 Spinner（供流式 UI 断言）。"""

    def __init__(self):
        self.started = False
        self.shown = False

    def start(self):
        self.started = True

    def stop(self):
        self.started = False

    def show(self):
        self.shown = True

    def hide(self):
        self.shown = False


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
    """内存会话存储：load_conversation 返回 SimpleNamespace，list_conversations
    返回元数据摘要（``navigate_conversation``/``_get_sorted_conversations`` 依赖）。
    """

    def __init__(self, conversations, summaries=None):
        self._conversations = conversations  # conv_id -> dict(messages=[...], ...)
        self._summaries = summaries          # 显式摘要（可覆盖派生值）

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

    def list_conversations(self):
        if self._summaries is not None:
            return list(self._summaries)
        return [
            {
                "id": cid,
                "title": spec.get("title", "New Conversation"),
                "message_count": len(spec.get("messages", [])),
                "updated_at": spec.get("updated_at", 0),
            }
            for cid, spec in self._conversations.items()
        ]

    def save_conversation(self, conv, bump_updated_at=True):
        pass


def _streaming_state(req_id=5):
    """正在流式的未落盘会话状态：含未解决的 bash 工具调用 + 推理文本。

    与 test_ai_switch_back_restore 的夹具同构；``response_div_added=True``
    表示切走前容器已渲染——切回时必须被 ``_rebind_active_stream`` 复位。
    """
    return {
        "streaming": True,
        "req_id": req_id,
        "request_key": ("ai", "convA", req_id),
        "cancel_event": threading.Event(),
        "messages": [
            {"role": "user", "content": "帮我分析一下 unsaved 会话"},
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
        "response_div_added": True,  # 切回后成为陈旧标记（必须被复位）
        "ai_markdown_text": "<p>部分渲染内容</p>",
    }


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
    panel._ai_active_model_info = None
    panel._ai_last_prompt_obj = None
    panel._ai_cancelling = False
    panel._ai_render_timeout_id = 0
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


# ═══════════════════════════════════════════════════════════════════
#  (a) 直接切换到未落盘的流式会话（核心 bug）
# ═══════════════════════════════════════════════════════════════════


class TestSwitchToUnsavedRunningConversation(unittest.TestCase):
    """bug：``_switch_to_conversation`` 对磁盘上不存在的流式会话提前 return。"""

    def test_switch_restores_unsaved_streaming_state(self):
        """新建会话正在流式、尚未持久化时切换：面板必须恢复流式会话。

        convA 只存在于 ``_ai_running_convs``（磁盘 store 无此 id）；当前面板在
        磁盘会话 convB。切换后必须成为 convA 的流式视图（当前：提前 return）。
        """
        panel = _make_panel()
        st = _streaming_state()
        panel._ai_running_convs = {"convA": st}
        panel._ai_conversation_id = "convB"
        panel._ai_messages = [{"role": "user", "content": "B 的问题"}]
        panel._ai_conversation_created_at = 999
        panel._ai_streaming = False
        panel._ai_request_id = 5
        panel._ai_response_div_added = True   # 陈旧标记：切回后必须被复位
        panel._streaming_container_created = True
        panel._ai_html_cache = {
            "convA": "<div id='content'>A 的部分流式渲染</div>",
            "convB": "<div id='content'>B 的历史消息</div>",
        }
        panel._ai_send_btn.label = "发送"
        panel._conversation_store = _FakeConversationStore({
            "convB": {"messages": [{"role": "user", "content": "B 的问题"}],
                      "created_at": 999},
        })  # convA 未落盘 → load_conversation("convA") 返回 None

        AIChatPanel._switch_to_conversation(panel, "convA")

        # 核心：目标会话与消息必须从运行态恢复（当前提前 return → FAIL）
        self.assertEqual(
            panel._ai_conversation_id, "convA",
            "未落盘的流式会话切换后 _ai_conversation_id 必须指向目标会话",
        )
        self.assertIs(
            panel._ai_messages, st["messages"],
            "切换后面板消息必须镜像运行态消息（同对象）",
        )
        self.assertTrue(
            panel._ai_streaming,
            "未落盘流式会话切换后 _ai_streaming 必须为 True",
        )
        # 陈旧 DOM 标记复位 → 下次渲染 tick 重新 appendMessageContainer
        self.assertFalse(
            panel._ai_response_div_added,
            "切回后 _ai_response_div_added 必须被复位（陈旧标记）",
        )
        self.assertFalse(
            panel._streaming_container_created,
            "切回后 _streaming_container_created 必须被复位",
        )
        # 流式 UI 状态：暂停按钮 / 等待占位符 / spinner
        self.assertEqual(
            panel._ai_send_btn.label, AI_BTN_LABEL_STOP,
            "切换后发送按钮必须显示流式「暂停」标签",
        )
        self.assertEqual(
            panel._ai_entry.placeholder_text, "等待回复中...",
            "切换后输入框必须显示流式等待占位符",
        )
        self.assertTrue(
            panel._ai_spinner.started and panel._ai_spinner.shown,
            "切换后 spinner 必须显示并旋转",
        )
        # 缓存的流式渲染 HTML 必须推送到 WebView（updateContent 重建 #content）
        self.assertIn(
            "updateContent", "\n".join(panel._ai_webview.js_calls),
            "切换后必须把缓存 HTML 推送到 WebView（当前提前 return 无任何 JS）",
        )


# ═══════════════════════════════════════════════════════════════════
#  (b) navigate_conversation 键盘路径
# ═══════════════════════════════════════════════════════════════════


class TestNavigateToUnsavedRunningConversation(unittest.TestCase):
    """键盘导航到未落盘流式会话：必须真正切换（当前在 _switch 处提前 return）。"""

    def test_navigate_to_unsaved_streaming_conversation(self):
        """排序列表含未落盘流式 convA（最新）；从 convB 按 Up(-1) 切到 convA。"""
        panel = _make_panel()
        st = _streaming_state()
        panel._ai_running_convs = {"convA": st}
        panel._ai_conversation_id = "convB"
        panel._ai_messages = [{"role": "user", "content": "B 的问题"}]
        panel._ai_streaming = False
        panel._ai_html_cache = {
            "convA": "<div id='content'>A 的部分流式渲染</div>",
            "convB": "<div id='content'>B 的历史消息</div>",
        }
        panel._conversation_store = _FakeConversationStore(
            {
                "convB": {"messages": [{"role": "user", "content": "B 的问题"}],
                          "updated_at": 300},
                "convC": {"messages": [{"role": "user", "content": "C 的问题"}],
                          "updated_at": 200},
            },
            # 显式摘要：convA 不在磁盘，由 _get_sorted_conversations 附加（最新）
            summaries=[
                {"id": "convB", "title": "B", "message_count": 1, "updated_at": 300},
                {"id": "convC", "title": "C", "message_count": 1, "updated_at": 200},
            ],
        )

        # 排序（DESC）：[convA(now), convB(300), convC(200)] → Up 从 convB 到 convA
        AIChatPanel.navigate_conversation(panel, -1)

        self.assertEqual(
            panel._ai_conversation_id, "convA",
            "键盘导航到未落盘流式会话后必须切换（当前 _switch 提前 return）",
        )
        self.assertIs(
            panel._ai_messages, st["messages"],
            "导航后消息必须来自运行态会话",
        )
        self.assertTrue(
            panel._ai_streaming,
            "导航到流式会话后 _ai_streaming 必须为 True",
        )


# ═══════════════════════════════════════════════════════════════════
#  (c) 非流式缺失目标：切换必须为 no-op（守卫）
# ═══════════════════════════════════════════════════════════════════


class TestSwitchMissingTargetNoop(unittest.TestCase):
    """目标既不在磁盘也不在运行态时，切换必须保持原会话不变（当前已通过）。"""

    def test_switch_to_unknown_conversation_is_noop(self):
        panel = _make_panel()
        panel._ai_running_convs = {}
        panel._ai_conversation_id = "convB"
        panel._ai_messages = [{"role": "user", "content": "B 的问题"}]
        panel._ai_streaming = False
        panel._ai_request_id = 7
        panel._ai_send_btn.label = "发送"
        panel._conversation_store = _FakeConversationStore({
            "convB": {"messages": [{"role": "user", "content": "B 的问题"}]},
        })

        AIChatPanel._switch_to_conversation(panel, "ghost")

        # 不变式：当前会话/消息/流式标记/按钮/JS 输出全部保持原样
        self.assertEqual(panel._ai_conversation_id, "convB")
        self.assertEqual(panel._ai_messages[0]["content"], "B 的问题")
        self.assertFalse(panel._ai_streaming)
        self.assertEqual(panel._ai_send_btn.label, "发送")
        self.assertEqual(
            panel._ai_webview.js_calls, [],
            "缺失目标的切换不得推送任何渲染 JS",
        )


# ═══════════════════════════════════════════════════════════════════
#  (d) 已落盘流式会话的 created_at 保留（守卫）
# ═══════════════════════════════════════════════════════════════════


class TestOnDiskStreamingCreatedAtPreserved(unittest.TestCase):
    """已落盘且正在流式的会话切换后，created_at 必须保留磁盘值（当前已通过）。"""

    def test_switch_to_ondisk_streaming_keeps_disk_created_at(self):
        panel = _make_panel()
        st = _streaming_state()
        panel._ai_running_convs = {"convA": st}
        panel._ai_conversation_id = "convB"
        panel._ai_messages = [{"role": "user", "content": "B 的问题"}]
        panel._ai_streaming = False
        panel._ai_conversation_created_at = 999
        panel._ai_html_cache = {
            "convA": "<div id='content'>A 的部分流式渲染</div>",
            "convB": "<div id='content'>B 的历史消息</div>",
        }
        panel._conversation_store = _FakeConversationStore({
            "convA": {"messages": [{"role": "user", "content": "A 的问题"}],
                      "created_at": 1234567},
            "convB": {"messages": [{"role": "user", "content": "B 的问题"}],
                      "created_at": 999},
        })

        AIChatPanel._switch_to_conversation(panel, "convA")

        self.assertEqual(
            panel._ai_conversation_created_at, 1234567,
            "流式会话切换后 created_at 必须保留磁盘值（不得被覆盖）",
        )
        self.assertEqual(panel._ai_conversation_id, "convA")
        self.assertIs(panel._ai_messages, st["messages"])
        self.assertTrue(panel._ai_streaming)


# ═══════════════════════════════════════════════════════════════════
#  (e) _get_sorted_conversations 包含未落盘流式会话（锚定）
# ═══════════════════════════════════════════════════════════════════


class TestSortedConversationsIncludeUnsavedRunning(unittest.TestCase):
    """排序列表必须包含未落盘的流式会话（导航能找到目标的前提，当前已通过）。"""

    def test_sorted_list_includes_unsaved_running_conversation(self):
        panel = _make_panel()
        st = _streaming_state()
        panel._ai_running_convs = {"convA": st}
        panel._ai_conversation_id = "convB"
        panel._ai_messages = [{"role": "user", "content": "B 的问题"}]
        panel._conversation_store = _FakeConversationStore(
            {
                "convB": {"messages": [{"role": "user", "content": "B 的问题"}],
                          "updated_at": 100},
                "convC": {"messages": [{"role": "user", "content": "C 的问题"}],
                          "updated_at": 50},
            },
            summaries=[
                {"id": "convB", "title": "B", "message_count": 1, "updated_at": 100},
                {"id": "convC", "title": "C", "message_count": 1, "updated_at": 50},
            ],
        )

        summaries = AIChatPanel._get_sorted_conversations(panel)

        ids = [s.get("id") for s in summaries]
        self.assertIn(
            "convA", ids,
            "排序列表必须包含未落盘的流式会话（导航定位目标的前提）",
        )
        self.assertEqual(
            ids[0], "convA",
            "未落盘的流式会话必须按 updated_at 排在最前（最新）",
        )


if __name__ == "__main__":
    unittest.main()
