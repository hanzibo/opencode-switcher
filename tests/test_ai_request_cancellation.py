#!/usr/bin/env python3
"""AI 请求级取消接线测试（Wave2）。

背景：Wave1 为 ``_LLMHttpClient`` 增加了按 ``request_key`` 登记/取消活动流式
响应的能力，但主 ReAct 循环、摘要流与用户取消路径尚未接线——``_fire_timeout``
与面板取消都调用无键的 ``cancel_active_request()``，会取消**所有**活动流，
导致并行会话互相误伤。

本文件覆盖：
- ToolLoopContext.request_key → stream_chat_completion 的键传递（主 ReAct 流）。
- 看门狗 ``_fire_timeout`` 按 ctx.request_key 精确取消（不再无键取消全部）。
- 无键回退：ctx.request_key=None 时看门狗仍走旧语义（无键取消全部）。
- 摘要流：request_key 传递 + 摘要看门狗按键强关。
- 面板 ``_cancel_streams_for_conversation``：定向取消只碰本会话（主流 + 摘要）。
- 并行会话持独立键，取消一个不影响另一个（client 层，面板式元组键）。
"""
import threading
import unittest
from types import SimpleNamespace
from unittest import mock

from ai_engine.llm_client import _LLMHttpClient, LLMRequestConfig
from ai_engine.ai_tool_loop import ToolLoopContext, run_llm_react_loop
from system.event_types import StreamEventType, text_delta, stream_end

# 复用 test_llm_client_concurrency 的确定性假 HTTP 响应基础设施
from tests.test_llm_client_concurrency import FakeResponse, FakeSession, _collect

from views.ai_chat_panel import (
    AIChatPanel,
    _ai_stream_request_key,
    _ai_summary_request_key,
)
from stores.clipboard_store import _DEFAULT_SUMMARY_TEMPLATE


def _make_config():
    return LLMRequestConfig(
        base_url="https://example.com/v1",
        api_key="test-key",
        model_name="deepseek-v4-flash",
    )


def _make_client(responses):
    fake = FakeSession(responses)
    with mock.patch("ai_engine.llm_client.requests.Session", return_value=fake):
        client = _LLMHttpClient()
    return client, fake


class FakeTimer:
    """替换 threading.Timer 的可控假定时器：捕获回调供测试手动触发。"""

    instances = []

    def __init__(self, timeout, callback):
        self.timeout = timeout
        self.callback = callback
        self.daemon = False
        self._cancelled = False
        FakeTimer.instances.append(self)

    def start(self):
        pass

    def cancel(self):
        self._cancelled = True


class TestRequestKeyHelpers(unittest.TestCase):
    def test_stream_key_format_and_uniqueness(self):
        key_a1 = _ai_stream_request_key("convA", 1)
        self.assertEqual(key_a1, ("ai", "convA", 1))
        # 重试递增 req_id → 新键，不与旧流冲突
        self.assertNotEqual(key_a1, _ai_stream_request_key("convA", 2))
        # 并行会话键互不相同
        self.assertNotEqual(key_a1, _ai_stream_request_key("convB", 1))

    def test_summary_key_scoped_to_conversation(self):
        self.assertEqual(_ai_summary_request_key("convA"), ("summary", "convA"))
        self.assertNotEqual(
            _ai_summary_request_key("convA"), _ai_summary_request_key("convB")
        )
        self.assertNotEqual(_ai_summary_request_key("convA"), ("ai", "convA", 1))


class TestReActLoopRequestKeyWiring(unittest.TestCase):
    """主 ReAct 循环：request_key 必须传递到 stream_chat_completion。"""

    def _ctx(self, llm, cancel_event, on_llm_error_fn=None, request_key=("ai", "conv1", 7)):
        return ToolLoopContext(
            req_id=7,
            cancel_event=cancel_event,
            get_current_request_id_fn=lambda: 7,
            append_message_fn=lambda m: None,
            append_html_to_webview_fn=lambda h: None,
            handle_ask_user_question_fn=lambda d: "",
            on_llm_api_finished_fn=lambda rid: None,
            finalize_after_tool_loop_fn=lambda rid: None,
            set_tool_iteration_fn=lambda v: None,
            reset_iteration_state_fn=lambda: None,
            on_llm_error_fn=on_llm_error_fn,
            conv_id="conv1",
            request_key=request_key,
        )

    def test_loop_passes_request_key_to_stream(self):
        """ReAct 循环调用 stream_chat_completion 时携带 ctx.request_key。"""
        llm = mock.MagicMock()
        llm.stream_chat_completion.return_value = iter([text_delta("hi"), stream_end()])
        cancel_event = threading.Event()
        ctx = self._ctx(llm, cancel_event)

        run_llm_react_loop(llm, _make_config(), ctx, [{"role": "user", "content": "hi"}])

        self.assertEqual(llm.stream_chat_completion.call_count, 1)
        kwargs = llm.stream_chat_completion.call_args.kwargs
        self.assertEqual(kwargs["request_key"], ("ai", "conv1", 7))
        # 正常完成路径不应触发取消
        llm.cancel_active_request.assert_not_called()

    @mock.patch("ai_engine.ai_tool_loop._LLM_FIRST_TOKEN_TIMEOUT_SEC", 0.05)
    def test_watchdog_cancels_targeted_key(self):
        """看门狗超时按 ctx.request_key 精确取消，而不是无键取消全部。"""
        llm = mock.MagicMock()
        cancel_event = threading.Event()
        on_llm_error = mock.MagicMock()
        ctx = self._ctx(llm, cancel_event, on_llm_error_fn=on_llm_error)

        def blocking_stream(*args, **kwargs):
            kwargs["cancel_event"].wait(3)
            return
            yield  # pragma: no cover — 使函数成为生成器

        llm.stream_chat_completion.side_effect = blocking_stream

        run_llm_react_loop(llm, _make_config(), ctx, [{"role": "user", "content": "hi"}])

        # 看门狗通过 cancel_active_request(request_key) 强关本会话响应
        llm.cancel_active_request.assert_called_once_with(("ai", "conv1", 7))
        # 超时原因上报错误回调
        on_llm_error.assert_called_once()
        self.assertTrue(cancel_event.is_set())
        # 流被取消 → 未追加工具调用 → 循环终止
        llm.stream_chat_completion.assert_called_once()

    @mock.patch("ai_engine.ai_tool_loop._LLM_FIRST_TOKEN_TIMEOUT_SEC", 0.05)
    def test_watchdog_no_key_falls_back_to_cancel_all(self):
        """无键回退：request_key=None 时看门狗保持旧语义（无键取消全部）。"""
        llm = mock.MagicMock()
        cancel_event = threading.Event()
        ctx = self._ctx(llm, cancel_event, request_key=None)

        def blocking_stream(*args, **kwargs):
            kwargs["cancel_event"].wait(3)
            return
            yield  # pragma: no cover

        llm.stream_chat_completion.side_effect = blocking_stream

        run_llm_react_loop(llm, _make_config(), ctx, [{"role": "user", "content": "hi"}])

        llm.cancel_active_request.assert_called_once_with()


class TestPanelTargetedCancel(unittest.TestCase):
    """面板取消路径：定向取消只碰目标会话，不误伤并行会话。"""

    def _fake_panel(self, running_convs, llm_client):
        fake = SimpleNamespace(
            _ai_running_convs=running_convs,
            _llm_client=llm_client,
        )
        return fake

    def test_cancel_streams_sets_event_and_cancels_both_keys(self):
        """取消会话 A：置位 cancel_event + 按 A 的主流键与摘要键精确取消。"""
        llm = mock.MagicMock()
        cancel_a = threading.Event()
        cancel_b = threading.Event()
        convs = {
            "convA": {
                "cancel_event": cancel_a,
                "request_key": ("ai", "convA", 1),
                "streaming": True,
            },
            "convB": {
                "cancel_event": cancel_b,
                "request_key": ("ai", "convB", 2),
                "streaming": True,
            },
        }
        fake = self._fake_panel(convs, llm)

        ret = AIChatPanel._cancel_streams_for_conversation(fake, "convA")

        self.assertTrue(ret)
        self.assertTrue(cancel_a.is_set())
        self.assertFalse(cancel_b.is_set(), "并行会话 B 的 cancel_event 不应被触碰")
        llm.cancel_active_request.assert_has_calls(
            [mock.call(("ai", "convA", 1)), mock.call(("summary", "convA"))]
        )

    def test_cancel_streams_absent_conversation_returns_false(self):
        """目标会话不在 running_convs 中 → 返回 False，不执行任何取消。"""
        llm = mock.MagicMock()
        fake = self._fake_panel({}, llm)

        ret = AIChatPanel._cancel_streams_for_conversation(fake, "ghost")

        self.assertFalse(ret)
        llm.cancel_active_request.assert_not_called()

    def test_cancel_streams_missing_request_key_still_sets_event(self):
        """状态里没有 request_key（旧状态兼容）→ 仍置位 cancel_event，跳过主流键。"""
        llm = mock.MagicMock()
        cancel_a = threading.Event()
        convs = {"convA": {"cancel_event": cancel_a}}
        fake = self._fake_panel(convs, llm)

        ret = AIChatPanel._cancel_streams_for_conversation(fake, "convA")

        self.assertTrue(ret)
        self.assertTrue(cancel_a.is_set())
        # 没有主流键 → 只取消摘要键
        llm.cancel_active_request.assert_called_once_with(("summary", "convA"))


class TestSummaryStreamRequestKey(unittest.TestCase):
    """摘要流：request_key 传递 + 看门狗按键强关。"""

    def _fake_panel(self, llm_client):
        fake = SimpleNamespace(
            _ai_settings_store=SimpleNamespace(
                summary_max_chars=500,
                summary_prompt_template=_DEFAULT_SUMMARY_TEMPLATE,
            ),
            _ai_summary="",
            _ai_conversation_id="conv1",
            _ai_summary_generating=True,
            _ai_last_prompt_obj=None,
            _llm_client=llm_client,
        )
        fake._read_model_config = lambda *a, **k: (
            "https://example.com/v1", "key", "model", "alias",
            0.3, 4096, 1.0, False, "high",
        )
        fake._update_summary_display = lambda text: None
        fake._apply_prune = lambda *a, **k: None
        fake._show_summary_failure = lambda *a, **k: None
        return fake

    def test_summary_stream_receives_summary_key(self):
        """摘要流调用 stream_chat_completion 时携带 ("summary", conv_id) 键。"""
        llm = mock.MagicMock()

        def summary_stream(*args, **kwargs):
            self.assertEqual(kwargs.get("request_key"), ("summary", "conv1"))
            yield text_delta("压缩摘要")
            yield stream_end()

        llm.stream_chat_completion.side_effect = summary_stream
        fake = self._fake_panel(llm)

        AIChatPanel._generate_summary_async(fake, [{"role": "user", "content": "old"}], 10)

        llm.stream_chat_completion.assert_called_once()
        # 正常完成 → 无看门狗取消，摘要已保存
        llm.cancel_active_request.assert_not_called()
        self.assertIn("压缩摘要", fake._ai_summary)
        self.assertFalse(fake._ai_summary_generating)

    def test_summary_watchdog_cancels_by_key(self):
        """摘要总超时看门狗：置位 cancel_event 并按 summary 键强关流。"""
        llm = mock.MagicMock()
        started = threading.Event()
        fake = self._fake_panel(llm)

        def blocking_stream(*args, **kwargs):
            started.set()
            kwargs["cancel_event"].wait(3)
            return
            yield  # pragma: no cover

        llm.stream_chat_completion.side_effect = blocking_stream

        with mock.patch("views.ai_chat_panel.threading.Timer", FakeTimer):
            FakeTimer.instances = []
            t = threading.Thread(
                target=AIChatPanel._generate_summary_async,
                args=(fake, [{"role": "user", "content": "old"}], 10),
            )
            t.start()
            self.assertTrue(started.wait(5), "摘要流未进入读取")
            # 首个 Timer 即 total_timer（总超时硬限制），手动触发其回调
            FakeTimer.instances[0].callback()
            t.join(5)
            self.assertFalse(t.is_alive())

        # 看门狗按键强关摘要流
        llm.cancel_active_request.assert_called_once_with(("summary", "conv1"))
        self.assertFalse(fake._ai_summary_generating)


class TestParallelConversationsIndependentAtClient(unittest.TestCase):
    """client 层：面板式元组键下，取消一个会话不影响另一个。"""

    def test_cancel_conv_a_leaves_conv_b_intact(self):
        gate_b = threading.Event()
        gate_a = threading.Event()  # 永不放行，只靠取消 close 退出
        arrived_a = threading.Event()
        arrived_b = threading.Event()
        resp_a = FakeResponse(
            ['data: {"choices":[{"delta":{"content":"aaa"}}]}', "data: [DONE]"],
            gate=gate_a, arrived=arrived_a,
        )
        resp_b = FakeResponse(
            ['data: {"choices":[{"delta":{"content":"bbb"}}]}', "data: [DONE]"],
            gate=gate_b, arrived=arrived_b,
        )
        client, fake = _make_client([resp_a, resp_b])
        config = _make_config()
        cancel_a = threading.Event()
        results = {}

        key_a = ("ai", "convA", 1)
        key_b = ("ai", "convB", 2)

        def consume(tag, key, cancel_event):
            try:
                results[tag] = ("ok", _collect(
                    client.stream_chat_completion(
                        config, [], cancel_event=cancel_event, request_key=key,
                    ),
                ))
            except Exception as e:  # pragma: no cover
                results[tag] = ("error", e)

        t_a = threading.Thread(target=consume, args=("a", key_a, cancel_a))
        t_b = threading.Thread(target=consume, args=("b", key_b, None))
        t_a.start()
        t_b.start()

        self.assertTrue(arrived_a.wait(5), "流 A 未进入读取")
        self.assertTrue(arrived_b.wait(5), "流 B 未进入读取")

        # 真实取消序列：先置 cancel_event，再按 A 的键强关
        cancel_a.set()
        client.cancel_active_request(key_a)

        self.assertTrue(resp_a.closed, "A 的响应应被关闭")
        self.assertFalse(resp_b.closed, "B 的响应不应受影响")
        self.assertFalse(fake.closed, "存在其他活动请求时不得重建共享 session")
        with client._lock:
            self.assertNotIn(key_a, client._active_responses)
            self.assertIs(client._active_responses.get(key_b), resp_b)

        t_a.join(5)
        self.assertFalse(t_a.is_alive())
        self.assertEqual(results.get("a"), ("ok", ""))

        gate_b.set()
        t_b.join(5)
        self.assertFalse(t_b.is_alive())
        self.assertEqual(results.get("b"), ("ok", "bbb"))
        with client._lock:
            self.assertEqual(client._active_responses, {})


if __name__ == "__main__":
    unittest.main()
