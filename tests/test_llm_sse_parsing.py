#!/usr/bin/env python3
"""SSE 工具调用流解析回归测试（不入库，仅本地）。

覆盖 Ling-3.0-flash 等兼容实现的 None 值 chunk 场景：
OpenAI 标准省略键 vs 显式 {"id": null, "name": null} 两种风格均须安全。
"""
import unittest

from ai_engine.llm_client import _ToolCallAccumulator, parse_sse_events
from system.event_types import StreamEventType


class TestToolCallAccumulator(unittest.TestCase):

    def test_null_values_do_not_crash(self):
        """Ling 兼容实现：显式 null 的 id/name 不导致 TypeError（回归修复）。"""
        acc = _ToolCallAccumulator()
        acc.add_delta({"index": 0, "id": "call_1", "type": "function",
                       "function": {"name": "bash", "arguments": ""}})
        acc.add_delta({"index": 0, "id": None, "function": {"name": None, "arguments": "{}"}})  # 曾崩溃
        acc.add_delta({"index": 0, "id": None, "function": {"name": None, "arguments": ""}})
        calls = acc.get_calls()
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["id"], "call_1")
        self.assertEqual(calls[0]["function"]["name"], "bash")
        self.assertEqual(calls[0]["function"]["arguments"], "{}")

    def test_openai_omitted_keys_still_work(self):
        """OpenAI 标准：后续 chunk 省略键（回归，不允许破坏）。"""
        acc = _ToolCallAccumulator()
        acc.add_delta({"index": 0, "id": "call_x",
                       "function": {"name": "web_search", "arguments": ""}})
        acc.add_delta({"index": 0, "function": {"arguments": '{"q":'}})
        acc.add_delta({"index": 0, "function": {"arguments": '"hi"}'}})
        calls = acc.get_calls()
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["id"], "call_x")
        self.assertEqual(calls[0]["function"]["name"], "web_search")
        self.assertEqual(calls[0]["function"]["arguments"], '{"q":"hi"}')

    def test_function_null_skipped(self):
        """function 字段为 null 时安全跳过，不中断后续 chunk。"""
        acc = _ToolCallAccumulator()
        acc.add_delta({"index": 0, "id": "call_y",
                       "function": {"name": "bash", "arguments": ""}})
        acc.add_delta({"index": 0, "id": None, "function": None})  # 曾崩溃（NoneType）
        acc.add_delta({"index": 0, "id": None, "function": {"name": None, "arguments": "ok"}})
        calls = acc.get_calls()
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["id"], "call_y")
        self.assertEqual(calls[0]["function"]["name"], "bash")
        self.assertEqual(calls[0]["function"]["arguments"], "ok")

    def test_multi_index_interleaved(self):
        """多 index 交错 chunk：按 index 聚合、去重过滤、排序输出。"""
        acc = _ToolCallAccumulator()
        acc.add_delta({"index": 0, "id": "call_a", "function": {"name": "bash", "arguments": ""}})
        acc.add_delta({"index": 1, "id": "call_b", "function": {"name": "read_file", "arguments": ""}})
        acc.add_delta({"index": 0, "id": None, "function": {"name": None, "arguments": '{"cmd":"ls"}'}})
        acc.add_delta({"index": 1, "id": None, "function": {"name": None, "arguments": '{"path":"/tmp"}'}})
        calls = acc.get_calls()
        self.assertEqual([c["function"]["name"] for c in calls], ["bash", "read_file"])
        self.assertEqual(calls[0]["function"]["arguments"], '{"cmd":"ls"}')


class TestParseSSEEvents(unittest.TestCase):

    def _make_response(self, lines):
        class FakeResponse:
            def iter_lines(self, decode_unicode=True):
                return iter(lines)
        return FakeResponse()

    def test_parse_stream_with_null_tool_chunks(self):
        """集成：含 null 值 chunk 的完整 SSE 流可正常产出工具调用（曾中断）。"""
        lines = [
            'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_1","type":"function","function":{"name":"get_current_time","arguments":""}}]}}]}',
            'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":null,"function":{"name":null,"arguments":"{}"}}]}}]}',
            'data: {"choices":[{"delta":{"tool_calls":[{"index":1,"id":"call_2","type":"function","function":{"name":"web_search","arguments":""}}]}}]}',
            'data: {"choices":[{"delta":{"tool_calls":[{"index":1,"id":null,"function":{"name":null,"arguments":"{\\"query\\":\\"x\\"}"}}]}}]}',
            'data: {"choices":[{"delta":{},"finish_reason":"tool_calls"}]}',
            'data: [DONE]',
        ]
        events = list(parse_sse_events(self._make_response(lines)))
        tool_events = [e for e in events if e.type == StreamEventType.TOOL_CALLS]
        self.assertEqual(len(tool_events), 1)
        calls = tool_events[0].tool_calls
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0].name, "get_current_time")
        self.assertEqual(calls[0].arguments, "{}")
        self.assertEqual(calls[1].name, "web_search")
        self.assertEqual(calls[1].arguments, '{"query":"x"}')

    def test_parse_stream_omitted_keys_openai_style(self):
        """OpenAI 风格（省略键）：流式文本 + 工具调用正常解析。"""
        lines = [
            'data: {"choices":[{"delta":{"role":"assistant","content":"我来"}}]}',
            'data: {"choices":[{"delta":{"content":"查询"}}]}',
            'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_9","type":"function","function":{"name":"bash","arguments":""}}]}}]}',
            'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"{}"}}]}}]}',
            'data: {"choices":[{"delta":{},"finish_reason":"tool_calls"}]}',
        ]
        events = list(parse_sse_events(self._make_response(lines)))
        texts = [e.text_delta for e in events if e.type == StreamEventType.TEXT_DELTA]
        self.assertEqual("".join(texts), "我来查询")
        tool_events = [e for e in events if e.type == StreamEventType.TOOL_CALLS]
        self.assertEqual(len(tool_events), 1)
        self.assertEqual(tool_events[0].tool_calls[0].name, "bash")
        self.assertEqual(tool_events[0].tool_calls[0].arguments, "{}")

    def test_parse_stream_non_json_line_skipped(self):
        """非 JSON data 行静默跳过，不影响后续解析。"""
        lines = [
            'data: {"choices":[{"delta":{"content":"a"}}]}',
            'data: not-json-at-all',
            'data: {"choices":[{"delta":{"content":"b"}}]}',
            'data: [DONE]',
        ]
        events = list(parse_sse_events(self._make_response(lines)))
        texts = [e.text_delta for e in events if e.type == StreamEventType.TEXT_DELTA]
        self.assertEqual("".join(texts), "ab")

    # ── 审查修复回归：显式 null 全家族（🟡-1）与非 str 值（🟡-2） ──────

    def test_parse_stream_delta_null(self):
        """delta 字段为 null：跳过该 chunk，不中断流（曾崩溃）。"""
        lines = [
            'data: {"choices":[{"delta":null}]}',
            'data: {"choices":[{"delta":{"content":"ok"}}]}',
            'data: [DONE]',
        ]
        events = list(parse_sse_events(self._make_response(lines)))
        texts = [e.text_delta for e in events if e.type == StreamEventType.TEXT_DELTA]
        self.assertEqual("".join(texts), "ok")

    def test_parse_stream_choices_null_element(self):
        """choices 数组含 null 元素：跳过，不中断流（曾崩溃）。"""
        lines = [
            'data: {"choices":[null]}',
            'data: {"choices":[{"delta":{"content":"ok"}}]}',
            'data: [DONE]',
        ]
        events = list(parse_sse_events(self._make_response(lines)))
        texts = [e.text_delta for e in events if e.type == StreamEventType.TEXT_DELTA]
        self.assertEqual("".join(texts), "ok")

    def test_parse_stream_tool_calls_null_element(self):
        """tool_calls 数组含 null 元素：跳过，不中断流（曾崩溃）。"""
        lines = [
            'data: {"choices":[{"delta":{"tool_calls":[null]}}]}',
            'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_1","function":{"name":"bash","arguments":""}}]}}]}',
            'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":null,"function":{"name":null,"arguments":"{}"}}]}}]}',
            'data: {"choices":[{"delta":{},"finish_reason":"tool_calls"}]}',
            'data: [DONE]',
        ]
        events = list(parse_sse_events(self._make_response(lines)))
        tool_events = [e for e in events if e.type == StreamEventType.TOOL_CALLS]
        self.assertEqual(len(tool_events), 1)
        self.assertEqual(tool_events[0].tool_calls[0].name, "bash")

    def test_parse_stream_choices_not_list(self):
        """choices 为非 list（null/字符串）：跳过，不中断流。"""
        lines = [
            'data: {"choices":null}',
            'data: {"choices":"oops"}',
            'data: {"choices":[{"delta":{"content":"ok"}}]}',
            'data: [DONE]',
        ]
        events = list(parse_sse_events(self._make_response(lines)))
        texts = [e.text_delta for e in events if e.type == StreamEventType.TEXT_DELTA]
        self.assertEqual("".join(texts), "ok")

    def test_parse_stream_non_str_arguments_skipped(self):
        """arguments/id/name 为 dict/int 等非 str：跳过该字段，不中断流（🟡-2）。"""
        lines = [
            'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_1","function":{"name":"bash","arguments":""}}]}}]}',
            'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":123,"function":{"name":456,"arguments":{"cmd":"ls"}}}]}}]}',
            'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":null,"function":{"name":null,"arguments":"{}"}}]}}]}',
            'data: {"choices":[{"delta":{},"finish_reason":"tool_calls"}]}',
            'data: [DONE]',
        ]
        events = list(parse_sse_events(self._make_response(lines)))
        tool_events = [e for e in events if e.type == StreamEventType.TOOL_CALLS]
        self.assertEqual(len(tool_events), 1)
        call = tool_events[0].tool_calls[0]
        self.assertEqual(call.name, "bash")
        self.assertEqual(call.arguments, "{}")  # 非 str 的 id=123/name=456 被跳过，未污染

    def test_done_flushes_unfinished_calls(self):
        """[DONE] 到达时仍有未 flush 的工具调用：正常产出。"""
        lines = [
            'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_x","function":{"name":"get_current_time","arguments":""}}]}}]}',
            'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":null,"function":{"name":null,"arguments":"{}"}}]}}]}',
            'data: [DONE]',
        ]
        events = list(parse_sse_events(self._make_response(lines)))
        tool_events = [e for e in events if e.type == StreamEventType.TOOL_CALLS]
        self.assertEqual(len(tool_events), 1)
        self.assertEqual(tool_events[0].tool_calls[0].name, "get_current_time")

    def test_stream_natural_end_flushes_calls(self):
        """流自然结束（无 [DONE]）：fallback flush 残留工具调用。"""
        lines = [
            'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_y","function":{"name":"web_search","arguments":""}}]}}]}',
            'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":null,"function":{"name":null,"arguments":"{\\"q\\":\\"hi\\"}"}}]}}]}',
            # 无 [DONE]，iter_lines 直接耗尽
        ]
        events = list(parse_sse_events(self._make_response(lines)))
        tool_events = [e for e in events if e.type == StreamEventType.TOOL_CALLS]
        self.assertEqual(len(tool_events), 1)
        self.assertEqual(tool_events[0].tool_calls[0].name, "web_search")
        self.assertEqual(tool_events[0].tool_calls[0].arguments, '{"q":"hi"}')

    def test_usage_only_chunk_skipped(self):
        """usage-only chunk（空 choices）：跳过不崩溃。"""
        lines = [
            'data: {"choices":[],"usage":{"total_tokens":10}}',
            'data: {"choices":[{"delta":{"content":"ok"}}]}',
            'data: [DONE]',
        ]
        events = list(parse_sse_events(self._make_response(lines)))
        texts = [e.text_delta for e in events if e.type == StreamEventType.TEXT_DELTA]
        self.assertEqual("".join(texts), "ok")

    def test_cancel_event_silent_on_read_error(self):
        """取消置位后读取异常：静默收尾，不抛 _LLMHttpError（🟡-4）。"""
        import threading
        from ai_engine.llm_client import _LLMHttpError

        class BoomResponse:
            def iter_lines(self, decode_unicode=True):
                yield 'data: {"choices":[{"delta":{"content":"a"}}]}'
                raise IOError("connection reset by peer (cancel close)")

        cancel_event = threading.Event()
        cancel_event.set()  # 用户已取消
        try:
            events = list(parse_sse_events(BoomResponse(), cancel_event=cancel_event))
        except _LLMHttpError:
            self.fail("取消后读取异常不应再抛 _LLMHttpError")
        self.assertEqual(events, [])  # 静默结束

    def test_cancel_event_not_set_still_raises(self):
        """未取消时读取异常：仍抛 _LLMHttpError（不误伤真实错误）。"""
        import threading
        from ai_engine.llm_client import _LLMHttpError

        class BoomResponse:
            def iter_lines(self, decode_unicode=True):
                yield 'data: {"choices":[{"delta":{"content":"a"}}]}'
                raise IOError("connection reset by peer")

        with self.assertRaises(_LLMHttpError):
            list(parse_sse_events(BoomResponse()))


if __name__ == "__main__":
    unittest.main()
