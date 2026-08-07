#!/usr/bin/env python3
"""主对话 AI 正式回答结束自动弹桌面通知的触发条件测试。

行为约定（add-ai-answer-notification）：
- 触发点：``_on_llm_api_finished`` 收尾阶段（``_handle_stream_end`` 之前）。
- 仅当以下条件**同时**满足才通知：
  (a) 流属于当前可见会话（``conv_id == self._ai_conversation_id``）；
  (b) 本轮有实际回答内容（``assistant_text or reasoning`` 非空）；
  (c) 设置开关 ``enable_answer_notification`` 为 True；
  (d) 无未消费的错误标志（``_render_llm_error`` 曾渲染错误气泡 → 跳过）。

覆盖：
- 正常回答 → 后台通知线程启动（标题/正文/参数正确）。
- 空回答（取消/无输出）→ 不通知。
- 开关关闭 → 不通知。
- 错误标志未消费 → 不通知并消费标志。
- 背景会话（conv != 当前）→ 不通知。

复用既有无头假面板模式：``AIChatPanel.__new__`` + 桩属性
（同 tests/test_ai_superseded_stream_finish.py）。
"""
import os
import unittest
from types import SimpleNamespace
from unittest import mock

os.environ.setdefault("GDK_BACKEND", "dummy")  # 无头环境导入 GTK

from views.ai_chat_panel import AIChatPanel, execute_send_notification


def _make_panel(**overrides):
    """构造无头假面板：仅提供通知判定所需的最小桩属性。"""
    panel = AIChatPanel.__new__(AIChatPanel)
    panel._ai_conversation_id = "conv1"
    panel._ai_settings_store = SimpleNamespace(enable_answer_notification=True)
    panel._ai_running_convs = {}
    panel._ai_cancelling = False
    panel._ai_messages = []
    panel._ai_request_id = 1
    panel._ai_title_generated = True
    panel._ai_streaming = False
    panel._ai_current_assistant_text = ""
    panel._ai_current_reasoning_text = ""
    panel._ai_assistant_buffer = ""
    panel._ai_spinner = mock.Mock()
    panel._ai_entry = SimpleNamespace(placeholder_text="")
    for k, v in overrides.items():
        setattr(panel, k, v)
    return panel


class TestAnswerFinishedNotification(unittest.TestCase):
    """``_on_llm_api_finished`` 的通知触发条件。"""

    def _call_finished(self, panel, req_id=1, conv_id="conv1", state=None,
                       current_conv=None):
        """直接执行 _on_llm_api_finished 的核心通知判定段（绕过完整收尾）。

        _on_llm_api_finished 依赖较多 UI 桩（spinner/button 等），这里仅调用
        通知判定逻辑：构造同样的运行态并触发，检查通知线程是否启动。
        ``conv_id`` 是运行流归属会话；``current_conv`` 是当前可见会话
        （缺省与运行流一致）。
        """
        panel._ai_running_convs = {
            conv_id: state or {
                "req_id": req_id,
                "current_assistant_text": "这是正式回答内容。",
                "current_reasoning_text": "",
                "streaming": False,
                "messages": [{"role": "user", "content": "hi"}],
                "response_div_added": True,
            }
        }
        panel._ai_conversation_id = current_conv or conv_id

        with mock.patch("views.ai_chat_panel.threading.Thread") as mock_thread:
            # 走 _on_llm_api_finished：校验 + 追加 + 通知段 + _handle_stream_end。
            # _handle_stream_end 依赖 save/render 等，这里替换为 no-op。
            with mock.patch.object(panel, "_handle_stream_end") as mock_end, \
                 mock.patch.object(panel, "_save_current_conversation"), \
                 mock.patch.object(panel, "_prune_messages"), \
                 mock.patch.object(panel, "_update_send_button"), \
                 mock.patch.object(panel, "_render_background_conversation"), \
                 mock.patch.object(panel, "_build_model_snapshot", return_value=None):
                panel._on_llm_api_finished(req_id)
        return mock_thread, mock_end

    def test_normal_answer_starts_notification_thread(self):
        """正常回答 → 后台通知线程启动，参数正确。"""
        panel = _make_panel()
        mock_thread, _ = self._call_finished(panel)
        self.assertEqual(mock_thread.call_count, 1)
        args, kwargs = mock_thread.call_args
        self.assertTrue(kwargs["daemon"])
        self.assertEqual(kwargs["target"], execute_send_notification)
        nkwargs = kwargs["kwargs"]
        self.assertEqual(nkwargs["summary"], "🤖 AI 回答完成")
        self.assertEqual(nkwargs["body"], "这是正式回答内容。")
        self.assertEqual(nkwargs["urgency"], "normal")
        self.assertEqual(nkwargs["icon"], "dialog-information")

    def test_empty_answer_does_not_notify(self):
        """空回答（取消/无输出）→ 不通知。"""
        panel = _make_panel()
        state = {
            "req_id": 1,
            "current_assistant_text": "",
            "current_reasoning_text": "",
            "streaming": False,
            "messages": [{"role": "user", "content": "hi"}],
            "response_div_added": True,
        }
        mock_thread, _ = self._call_finished(panel, state=state)
        self.assertEqual(mock_thread.call_count, 0)

    def test_reasoning_only_still_notifies(self):
        """仅推理内容（reasoning 非空）也视为有回答 → 通知。"""
        panel = _make_panel()
        state = {
            "req_id": 1,
            "current_assistant_text": "",
            "current_reasoning_text": "思考过程……",
            "streaming": False,
            "messages": [{"role": "user", "content": "hi"}],
            "response_div_added": True,
        }
        mock_thread, _ = self._call_finished(panel, state=state)
        self.assertEqual(mock_thread.call_count, 1)

    def test_disabled_setting_does_not_notify(self):
        """设置开关关闭 → 不通知。"""
        panel = _make_panel()
        panel._ai_settings_store = SimpleNamespace(enable_answer_notification=False)
        mock_thread, _ = self._call_finished(panel)
        self.assertEqual(mock_thread.call_count, 0)

    def test_pending_error_flag_suppresses_notification(self):
        """错误标志未消费 → 不通知，并消费标志。"""
        panel = _make_panel()
        panel._ai_error_pending_conv = "conv1"
        mock_thread, _ = self._call_finished(panel)
        self.assertEqual(mock_thread.call_count, 0)
        # 标志已被消费（置 None，不再等于 conv1）
        self.assertTrue(not hasattr(panel, "_ai_error_pending_conv")
                        or panel._ai_error_pending_conv is None)

    def test_background_conversation_does_not_notify(self):
        """背景会话（流属于非当前会话）→ 不通知。"""
        panel = _make_panel()
        # 运行流属于 conv2，但当前可见会话是 conv1
        mock_thread, _ = self._call_finished(panel, conv_id="conv2", current_conv="conv1")
        self.assertEqual(mock_thread.call_count, 0)

    def test_pause_cancel_snapshot_suppresses_notification(self):
        """暂停取消中（_ai_cancelling=True）即使有 partial 文本也不通知。"""
        panel = _make_panel(_ai_cancelling=True)
        mock_thread, _ = self._call_finished(panel)
        self.assertEqual(mock_thread.call_count, 0)

    def test_handle_stream_end_still_called(self):
        """通知不破坏收尾：_handle_stream_end 必须仍被调用。"""
        panel = _make_panel()
        mock_thread, mock_end = self._call_finished(panel)
        self.assertEqual(mock_thread.call_count, 1)
        mock_end.assert_called_once()

    def test_zero_output_error_flag_consumed_no_leak(self):
        """零输出错误（文本为空）：错误标志必须无条件消费，不残留到下一轮。

        场景：请求一开始就 HTTP 失败（assistant/reasoning 均为空），
        _render_llm_error 已置标志，但 _on_llm_api_finished 因文本为空
        不会走内层 if——若消费逻辑在外层条件内，标志残留到同会话下一轮
        成功回答，误判"错误未消费"而吞掉通知。
        """
        panel = _make_panel()
        panel._ai_error_pending_conv = "conv1"
        empty_state = {
            "req_id": 1,
            "current_assistant_text": "",
            "current_reasoning_text": "",
            "streaming": False,
            "messages": [{"role": "user", "content": "hi"}],
            "response_div_added": True,
        }
        mock_thread, _ = self._call_finished(panel, state=empty_state)
        self.assertEqual(mock_thread.call_count, 0)
        # 标志已被消费（无条件消费），不应残留
        self.assertTrue(not hasattr(panel, "_ai_error_pending_conv")
                        or panel._ai_error_pending_conv is None)

        # 同会话下一轮成功回答：标志已清 → 应正常通知
        panel._ai_error_pending_conv = None
        mock_thread2, _ = self._call_finished(panel)
        self.assertEqual(mock_thread2.call_count, 1)


class TestNotifyMethod(unittest.TestCase):
    """``_notify_ai_answer_finished`` 自身行为。"""

    def test_preview_truncated_to_80_chars(self):
        panel = _make_panel()
        long_text = "长" * 200
        with mock.patch("views.ai_chat_panel.threading.Thread") as mock_thread:
            panel._notify_ai_answer_finished(long_text)
        kwargs = mock_thread.call_args.kwargs["kwargs"]
        self.assertEqual(len(kwargs["body"]), 80)
        self.assertEqual(kwargs["summary"], "🤖 AI 回答完成")

    def test_exception_is_silent(self):
        """通知异常 → 静默（不抛出）。"""
        panel = _make_panel()
        with mock.patch("views.ai_chat_panel.threading.Thread",
                        side_effect=RuntimeError("boom")):
            # 不应抛异常
            panel._notify_ai_answer_finished("text")


if __name__ == "__main__":
    unittest.main()
