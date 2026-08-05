#!/usr/bin/env python3
"""RED 回归测试：面板发送/重试必须透传 thinking_enabled 与 reasoning_effort。

缺陷（见 docs/plans/ai-streaming-quality-phase1-plan.md §4）：``_send_user_message``
与 ``_retry_response`` 解包 ``_read_model_config()`` 的 9 元组时丢弃最后两个字段
（``thinking_enabled``、``reasoning_effort``），启动的 ``_run_llm_api_request`` 线程
只收到 11 个位置参数，永远落在签名默认值 ``False``/``"high"`` 上——与 ``ask_llm_api``
的完整 13 参数透传语义不一致。本文件为 RED 测试：修复前长度断言必然失败。

覆盖：
- (a) ``_send_user_message``：``_read_model_config`` 返回尾部 ``True``/``'low'`` → 线程
  args 尾部为 ``(True, 'low')``，且与 ``_run_llm_api_request`` 形参逐一映射。
- (b) ``_send_user_message`` 默认 case：尾部 ``False``/``'high'``。
- (c) ``_retry_response``：``True``/``'low'`` 透传。
- (d) ``_retry_response`` 默认 case：``False``/``'high'``。

复用既有无头假面板模式：``AIChatPanel.__new__`` + 桩属性 + 假 WebView
（同 tests/test_ai_superseded_stream_finish.py）。
"""
import inspect
import os
import unittest
from types import SimpleNamespace
from unittest import mock

os.environ.setdefault("GDK_BACKEND", "dummy")  # 无头环境导入 GTK

from views.ai_chat_panel import AIChatPanel


# ═══════════════════════════════════════════════════════════════════
#  无头假件（display-independent，同 test_ai_superseded_stream_finish）
# ═══════════════════════════════════════════════════════════════════


class _RecordingThread:
    """替换 threading.Thread：捕获实例化参数，start() 为 no-op。"""

    instances = []

    def __init__(self, *args, **kwargs):
        # 生产代码以关键字传参：Thread(target=..., args=(...), daemon=True)
        self.init_args = args
        self.kwargs = kwargs
        self.target = kwargs.get("target")
        self.args = kwargs.get("args", ())
        _RecordingThread.instances.append(self)

    def start(self):
        pass


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


class _FakeSpinner:
    def start(self):
        pass

    def stop(self):
        pass

    def show(self):
        pass

    def hide(self):
        pass


class _FakeEntry:
    def __init__(self):
        self.placeholder_text = ""


def _make_panel(model_config_tuple):
    """无 GTK 的假 AIChatPanel：__new__ + 桩属性（test_system_prompt 模式）。

    ``model_config_tuple`` 即 ``_read_model_config()`` 应返回的 9 元组；
    测试把桩返回值固定为被测入口，让断言聚焦在解包/透传语义上。
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
    panel._ai_assistant_html_base = ""
    panel._ai_streaming = False
    panel._ai_pending_image_hash = None
    panel._ai_last_prompt_obj = None
    panel._ai_active_model_info = None
    panel._ai_cancelling = False
    panel._ai_render_timeout_id = 0
    panel._ai_send_btn = _FakeButton()
    panel._ai_entry = _FakeEntry()
    panel._ai_spinner = _FakeSpinner()
    panel._mcp_initialized = True  # _init_mcp 幂等短路，跳过真实桥接启动
    panel._ai_settings_store = SimpleNamespace(
        soft_limit=500,
        trim_target=50,
        enable_summary=False,
        summary_threshold=3,
        enable_incremental_tools=False,
        disabled_tools=[],
        show_tool_details=True,
    )
    # 方法桩（与断言正交的重路径/IO）
    panel._snapshot_system_prompt = mock.Mock()
    panel._update_token_display = mock.Mock()
    panel._render_markdown = mock.Mock()
    panel._rebuild_markdown_from_messages = mock.Mock(return_value="")
    panel._build_llm_messages = mock.Mock(
        return_value=([{"role": "user", "content": "hi"}], None))
    panel._read_model_config = mock.Mock(return_value=model_config_tuple)
    return panel


def _model_config(thinking_enabled, reasoning_effort):
    """_read_model_config 的 9 元组桩：固定 base_url/api_key/model_name 非空以走通主路径。"""
    return ("https://example.com/v1", "test-key", "deepseek-v4-flash", "Test Alias",
            0.3, 4096, 1.0, thinking_enabled, reasoning_effort)


# ═══════════════════════════════════════════════════════════════════
#  断言辅助：线程 args 必须与 _run_llm_api_request 形参逐一映射
# ═══════════════════════════════════════════════════════════════════


def _assert_thinking_args(testcase, thread, thinking_enabled, reasoning_effort):
    """校验线程 args：长度与形参一致，尾部两参为 thinking_enabled/reasoning_effort。

    位置映射基于 ``_run_llm_api_request`` 的真实签名，与 ``ask_llm_api`` 透传的
    顺序（``..., extra_system_messages, thinking_enabled, reasoning_effort``）一致。
    """
    params = list(inspect.signature(AIChatPanel._run_llm_api_request).parameters)
    # 生产代码以绑定方法 self._run_llm_api_request 传参，self 不入 args
    params = params[1:]
    args = thread.args
    testcase.assertEqual(
        len(args), len(params),
        f"线程 args 长度 {len(args)} 必须等于 _run_llm_api_request 形参数 {len(params)}；"
        "说明 thinking_enabled/reasoning_effort 被丢弃",
    )
    mapping = dict(zip(params, args))
    testcase.assertIs(mapping["thinking_enabled"], thinking_enabled)
    testcase.assertEqual(mapping["reasoning_effort"], reasoning_effort)
    # 尾部顺序必须与 ask_llm_api 一致：args[-2]=thinking_enabled, args[-1]=reasoning_effort
    testcase.assertEqual(params[-2:], ["thinking_enabled", "reasoning_effort"])
    testcase.assertIs(args[-2], thinking_enabled)
    testcase.assertEqual(args[-1], reasoning_effort)


# ═══════════════════════════════════════════════════════════════════
#  核心回归：_send_user_message 透传思考配置
# ═══════════════════════════════════════════════════════════════════


class TestSendUserMessageThinkingPropagation(unittest.TestCase):
    """发送路径不得丢弃 _read_model_config 尾部的思考配置字段。"""

    def setUp(self):
        _RecordingThread.instances = []

    def test_send_propagates_thinking_enabled_and_reasoning_effort(self):
        """thinking=True / reasoning_effort='low' → 线程 args 尾部 (True, 'low')。"""
        panel = _make_panel(_model_config(True, "low"))

        with mock.patch("views.ai_chat_panel.threading.Thread", _RecordingThread):
            panel._send_user_message("hello")

        self.assertEqual(len(_RecordingThread.instances), 1)
        thread = _RecordingThread.instances[0]
        self.assertEqual(thread.target, panel._run_llm_api_request)
        _assert_thinking_args(self, thread, True, "low")

    def test_send_propagates_defaults_false_high(self):
        """默认 case（False/'high'）→ 线程 args 尾部 (False, 'high')，修复不得破坏默认。"""
        panel = _make_panel(_model_config(False, "high"))

        with mock.patch("views.ai_chat_panel.threading.Thread", _RecordingThread):
            panel._send_user_message("hello")

        self.assertEqual(len(_RecordingThread.instances), 1)
        _assert_thinking_args(self, _RecordingThread.instances[0], False, "high")


# ═══════════════════════════════════════════════════════════════════
#  核心回归：_retry_response 透传思考配置
# ═══════════════════════════════════════════════════════════════════


class TestRetryResponseThinkingPropagation(unittest.TestCase):
    """重试路径不得丢弃 _read_model_config 尾部的思考配置字段。"""

    def setUp(self):
        _RecordingThread.instances = []
        self.messages = [
            {"role": "user", "content": "问题"},
            {"role": "assistant", "content": "旧回答"},
        ]

    def _retry_panel(self, model_config_tuple):
        panel = _make_panel(model_config_tuple)
        panel._ai_messages = list(self.messages)
        return panel

    def test_retry_propagates_thinking_enabled_and_reasoning_effort(self):
        """thinking=True / reasoning_effort='low' → 线程 args 尾部 (True, 'low')。"""
        panel = self._retry_panel(_model_config(True, "low"))

        with mock.patch("views.ai_chat_panel.threading.Thread", _RecordingThread):
            panel._retry_response(1)

        self.assertEqual(len(_RecordingThread.instances), 1)
        thread = _RecordingThread.instances[0]
        self.assertEqual(thread.target, panel._run_llm_api_request)
        _assert_thinking_args(self, thread, True, "low")

    def test_retry_propagates_defaults_false_high(self):
        """默认 case（False/'high'）→ 线程 args 尾部 (False, 'high')。"""
        panel = self._retry_panel(_model_config(False, "high"))

        with mock.patch("views.ai_chat_panel.threading.Thread", _RecordingThread):
            panel._retry_response(1)

        self.assertEqual(len(_RecordingThread.instances), 1)
        _assert_thinking_args(self, _RecordingThread.instances[0], False, "high")


if __name__ == "__main__":
    unittest.main()
