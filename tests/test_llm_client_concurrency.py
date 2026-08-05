#!/usr/bin/env python3
"""_LLMHttpClient 并发流状态隔离与请求级取消测试。

覆盖：
- 同一客户端上两个并发流可共存（互不覆盖活动响应状态，含显式键 + 自动键混合）。
- 取消请求 A 只关闭 A 的响应，不影响 B。
- 连接阶段占位：存在其他活动请求时不重建共享 session，单独时则重建解除阻塞。
- 无键取消 = 旧语义的超集，关闭所有活动流。
- 所有路径结束后 _active_responses 均回到空（无悬挂引用）。
"""
import threading
import unittest
from unittest import mock

import requests

from ai_engine.llm_client import _LLMHttpClient, LLMRequestConfig
from system.event_types import StreamEventType


def _make_config():
    return LLMRequestConfig(
        base_url="https://example.com/v1",
        api_key="test-key",
        model_name="deepseek-v4-flash",
    )


class FakeResponse:
    """确定性假 HTTP 流式响应。

    ``gate`` 未 set 时 iter_lines 阻塞在产出首行之前，直到 gate 被 set
    或响应被 close（模拟"取消 close 解除 read 阻塞"的真实行为）。
    ``arrived`` 进入 iter_lines 后置位，供测试确认流已处于读取中。
    """

    def __init__(self, lines, gate=None, arrived=None):
        self._lines = list(lines)
        self._gate = gate
        self._arrived = arrived
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def raise_for_status(self):
        pass

    def iter_lines(self, decode_unicode=True):
        if self._arrived is not None:
            self._arrived.set()
        for line in self._lines:
            if self._gate is not None:
                while not self._gate.is_set() and not self.closed:
                    self._gate.wait(0.02)
            if self.closed:
                raise IOError("stream closed by cancel")
            yield line

    def close(self):
        self.closed = True


class FakeSession:
    """假 requests.Session：按序派发预设响应并记录调用。

    close 后 post 抛 ConnectionError（模拟真实 session 关闭后无法再发请求，
    用于验证"重建期间新注册的流不会被旧 session 误杀"）。
    """

    def __init__(self, responses):
        self._responses = list(responses)
        self._lock = threading.Lock()
        self.post_calls = []
        self.closed = False

    def mount(self, *args, **kwargs):
        pass

    def post(self, url, json=None, headers=None, stream=False, timeout=None):
        if self.closed:
            raise requests.exceptions.ConnectionError("session closed")
        with self._lock:
            self.post_calls.append((url, stream, timeout))
            resp = self._responses.pop(0)
        return resp

    def close(self):
        self.closed = True


class BlockingCloseSession(FakeSession):
    """close() 可阻塞的 FakeSession，用于模拟"旧 session 正在关闭"的窗口。

    ``close_entered`` 进入 close 后置位；随后阻塞在 ``close_gate`` 上
    （模拟真实 close 消耗时间），期间其他线程可注册新请求。
    """

    def __init__(self, responses, close_entered=None, close_gate=None):
        super().__init__(responses)
        self._close_entered = close_entered
        self._close_gate = close_gate

    def close(self):
        self.closed = True
        if self._close_entered is not None:
            self._close_entered.set()
        if self._close_gate is not None:
            self._close_gate.wait(10)


def _collect(gen):
    """迭代流式生成器，返回拼接后的文本增量。"""
    texts = []
    for event in gen:
        if event.type == StreamEventType.TEXT_DELTA and event.text_delta:
            texts.append(event.text_delta)
    return "".join(texts)


class TestLLMClientConcurrency(unittest.TestCase):

    def _make_client(self, responses):
        fake = FakeSession(responses)
        with mock.patch("ai_engine.llm_client.requests.Session", return_value=fake):
            client = _LLMHttpClient()
        return client, fake

    def _make_client_with_sessions(self, sessions):
        """按序分发 session 的 patch（须由调用方持有至测试结束）。

        返回 (client, patch) —— patch 必须在整个测试期间保持激活，
        否则取消线程内 _swap_session_locked 的 requests.Session() 会
        落到真实 Session 上。
        """
        factory = iter(sessions)
        patcher = mock.patch(
            "ai_engine.llm_client.requests.Session",
            side_effect=lambda: next(factory),
        )
        patcher.start()
        client = _LLMHttpClient()
        return client, patcher

    # ── 两个并发流共存 ──────────────────────────────────────────────

    def test_two_simultaneous_streams_coexist(self):
        """显式键 + 自动键的两条流同时登记、各自完整跑完、零残留。"""
        gate = threading.Event()
        arrived_a = threading.Event()
        arrived_b = threading.Event()
        resp_a = FakeResponse(
            ['data: {"choices":[{"delta":{"content":"alpha"}}]}', "data: [DONE]"],
            gate=gate, arrived=arrived_a,
        )
        resp_b = FakeResponse(
            ['data: {"choices":[{"delta":{"content":"beta"}}]}', "data: [DONE]"],
            gate=gate, arrived=arrived_b,
        )
        client, fake = self._make_client([resp_a, resp_b])
        config = _make_config()
        results = {}

        def consume(tag, key):
            try:
                results[tag] = ("ok", _collect(
                    client.stream_chat_completion(config, [], request_key=key),
                ))
            except Exception as e:  # pragma: no cover
                results[tag] = ("error", e)

        t_a = threading.Thread(target=consume, args=("a", "A"))
        t_b = threading.Thread(target=consume, args=("b", None))  # 自动键
        t_a.start()
        t_b.start()

        self.assertTrue(arrived_a.wait(5), "流 A 未进入读取")
        self.assertTrue(arrived_b.wait(5), "流 B 未进入读取")

        # 两个响应此刻同时登记在客户端上（旧实现单槽位会互相覆盖）
        with client._lock:
            keys = set(client._active_responses)
            self.assertIn("A", keys)
            self.assertEqual(len(keys), 2)
            self.assertIn(resp_a, client._active_responses.values())
            self.assertIn(resp_b, client._active_responses.values())

        gate.set()
        t_a.join(5)
        t_b.join(5)
        self.assertFalse(t_a.is_alive())
        self.assertFalse(t_b.is_alive())
        self.assertEqual(results["a"][0], "ok")
        self.assertEqual(results["b"][0], "ok")
        # 各自拿到完整内容（线程↔响应分配顺序不确定，只断言集合相等）
        self.assertEqual(
            set([results["a"][1], results["b"][1]]), {"alpha", "beta"},
        )
        with client._lock:
            self.assertEqual(client._active_responses, {})
        self.assertFalse(fake.closed)  # 正常结束不应关闭共享 session

    # ── 请求级取消：只关 A，不动 B ──────────────────────────────────

    def test_scoped_cancel_closes_only_target(self):
        """取消请求 A 仅关闭 A 的响应，B 继续完整跑完。"""
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
        client, fake = self._make_client([resp_a, resp_b])
        config = _make_config()
        cancel_a = threading.Event()
        results = {}

        def consume(tag, key, cancel_event):
            try:
                results[tag] = ("ok", _collect(
                    client.stream_chat_completion(
                        config, [], cancel_event=cancel_event, request_key=key,
                    ),
                ))
            except Exception as e:  # pragma: no cover
                results[tag] = ("error", e)

        t_a = threading.Thread(target=consume, args=("a", "A", cancel_a))
        t_b = threading.Thread(target=consume, args=("b", "B", None))
        t_a.start()
        t_b.start()

        self.assertTrue(arrived_a.wait(5), "流 A 未进入读取")
        self.assertTrue(arrived_b.wait(5), "流 B 未进入读取")

        # 真实取消序列：先置 cancel_event，再强关响应
        cancel_a.set()
        client.cancel_active_request(request_key="A")

        self.assertTrue(resp_a.closed, "A 的响应应被关闭")
        self.assertFalse(resp_b.closed, "B 的响应不应受影响")
        self.assertFalse(fake.closed, "存在其他活动请求时不得重建共享 session")
        with client._lock:
            self.assertNotIn("A", client._active_responses)
            self.assertIs(client._active_responses.get("B"), resp_b)

        # A 静默收尾（cancel_event 置位 → parse_sse_events 不抛错）
        t_a.join(5)
        self.assertFalse(t_a.is_alive())
        self.assertEqual(results.get("a"), ("ok", ""))

        # B 继续正常完成
        gate_b.set()
        t_b.join(5)
        self.assertFalse(t_b.is_alive())
        self.assertEqual(results.get("b"), ("ok", "bbb"))
        with client._lock:
            self.assertEqual(client._active_responses, {})

    # ── 连接阶段占位与 session 重建守卫 ──────────────────────────────

    def test_connect_phase_cancel_with_other_active_skips_session_close(self):
        """目标处于连接阶段但存在其他活动流：不重建 session，不误伤其他流。"""
        gate_a = threading.Event()
        arrived_a = threading.Event()
        resp_a = FakeResponse(["data: [DONE]"], gate=gate_a, arrived=arrived_a)
        client, fake = self._make_client([resp_a])
        config = _make_config()
        cancel_a = threading.Event()
        results = {}

        def consume():
            try:
                results["a"] = ("ok", _collect(
                    client.stream_chat_completion(
                        config, [], cancel_event=cancel_a, request_key="A",
                    ),
                ))
            except Exception as e:  # pragma: no cover
                results["a"] = ("error", e)

        t_a = threading.Thread(target=consume)
        t_a.start()
        self.assertTrue(arrived_a.wait(5), "流 A 未进入读取")

        # 模拟请求 C 仍处于连接阶段（占位 None）
        with client._lock:
            client._active_responses["C"] = None

        client.cancel_active_request(request_key="C")

        self.assertFalse(fake.closed, "存在其他活动流时不得重建共享 session")
        self.assertFalse(resp_a.closed, "流 A 不应被误伤")
        with client._lock:
            self.assertNotIn("C", client._active_responses)
            self.assertIs(client._active_responses.get("A"), resp_a)

        # 收尾：取消 A 并放行
        cancel_a.set()
        client.cancel_active_request(request_key="A")
        self.assertTrue(resp_a.closed)
        gate_a.set()
        t_a.join(5)
        self.assertFalse(t_a.is_alive())
        with client._lock:
            self.assertEqual(client._active_responses, {})

    def test_connect_phase_cancel_alone_rebuilds_session(self):
        """唯一活动请求仍处于连接阶段：可安全重建 session 解除其阻塞。"""
        client, fake = self._make_client([])
        with client._lock:
            client._active_responses["C"] = None  # 连接阶段占位

        client.cancel_active_request(request_key="C")

        self.assertTrue(fake.closed, "无其他活动请求时应重建 session 解除阻塞")
        with client._lock:
            self.assertEqual(client._active_responses, {})
        # 重建后 session 可用（新实例可继续发出请求）
        self.assertIsNotNone(client._session)

    # ── 无键取消 = 旧语义超集 ───────────────────────────────────────

    def test_legacy_cancel_all_closes_all_active_streams(self):
        """省略 request_key 时关闭所有活动流（旧语义超集），无残留。"""
        gate_a = threading.Event()
        gate_b = threading.Event()
        arrived_a = threading.Event()
        arrived_b = threading.Event()
        resp_a = FakeResponse(["data: [DONE]"], gate=gate_a, arrived=arrived_a)
        resp_b = FakeResponse(["data: [DONE]"], gate=gate_b, arrived=arrived_b)
        client, fake = self._make_client([resp_a, resp_b])
        config = _make_config()
        cancel = threading.Event()
        cancel.set()

        def consume(key):
            for _ in client.stream_chat_completion(
                config, [], cancel_event=cancel, request_key=key,
            ):
                pass

        t_a = threading.Thread(target=consume, args=("A",))
        t_b = threading.Thread(target=consume, args=("B",))
        t_a.start()
        t_b.start()
        self.assertTrue(arrived_a.wait(5))
        self.assertTrue(arrived_b.wait(5))

        client.cancel_active_request()

        self.assertTrue(resp_a.closed)
        self.assertTrue(resp_b.closed)
        t_a.join(5)
        t_b.join(5)
        self.assertFalse(t_a.is_alive())
        self.assertFalse(t_b.is_alive())
        with client._lock:
            self.assertEqual(client._active_responses, {})

    # ── P1 回归：取消决策与 session 关闭之间的窗口 ──────────────────

    def test_new_stream_registered_during_session_close_not_killed(self):
        """旧 session 关闭期间新注册的流必须绑定新 session，不被误杀。

        回归：旧实现先判定 close_session 再释放锁后关闭 session，窗口内
        新注册的请求绑定到旧 session，被随之而来的关闭误杀。现在 session
        切换与判定在同一临界区完成，新请求必然拿到新 session。
        """
        close_entered = threading.Event()
        close_gate = threading.Event()
        resp_b = FakeResponse(
            ['data: {"choices":[{"delta":{"content":"beta"}}]}', "data: [DONE]"],
        )
        fake_old = BlockingCloseSession([], close_entered=close_entered, close_gate=close_gate)
        fake_new = FakeSession([resp_b])
        client, patcher = self._make_client_with_sessions([fake_old, fake_new])
        try:
            # 请求 C 处于连接阶段且是唯一活动请求 → 取消将重建 session
            with client._lock:
                client._active_responses["C"] = None

            def do_cancel():
                client.cancel_active_request(request_key="C")

            t = threading.Thread(target=do_cancel)
            t.start()
            self.assertTrue(close_entered.wait(5), "取消未进入旧 session 关闭阶段")

            # 关键窗口：旧 session 正在被关闭，此时新流 B 注册并完整消费——
            # 必须绑定已发布的新 session，正常完成
            text = _collect(client.stream_chat_completion(
                _make_config(), [], request_key="B",
            ))
            self.assertEqual(text, "beta")
            self.assertFalse(resp_b.closed)
            self.assertIs(client._session, fake_new)
            self.assertEqual(len(fake_new.post_calls), 1)
            self.assertEqual(len(fake_old.post_calls), 0)

            close_gate.set()
            t.join(5)
            self.assertFalse(t.is_alive())
            self.assertTrue(fake_old.closed)
            with client._lock:
                self.assertEqual(client._active_responses, {})
        finally:
            patcher.stop()


if __name__ == "__main__":
    unittest.main()
