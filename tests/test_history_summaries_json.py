#!/usr/bin/env python3
"""``_history_summaries_json`` 的 ts 字段回归测试（feat/ai-history-ts）。

新增字段契约：items.append 时附带 ``"ts": s.get("updated_at", 0)``（毫秒整数），
供后续 WebView header 历史下拉的搜索/时间渲染（Wave 2 Task 5）使用。

覆盖：
- (a) 解析后的 JSON 每项含 ``"ts"`` 且为 int（毫秒数量级 > 1e12），缺失
  ``updated_at`` 时兜底为 0。
- (b) 列表顺序按 ts 降序（与 ``_get_sorted_conversations`` 的倒序一致）。
- (c) label 截断（30 字符标题 → 22 + "..."）与 ``(N条)`` 格式不回归。

复用既有无头假面板模式：``AIChatPanel.__new__`` + 桩属性 + 假会话存储
（同 tests/test_ai_switch_unsaved_running.py）。
"""
import json
import os
import unittest

os.environ.setdefault("GDK_BACKEND", "dummy")  # 无头环境导入 GTK

from views.ai_chat_panel import AIChatPanel


class _FakeConversationStore:
    """内存会话存储：list_conversations 返回元数据摘要（含 updated_at）。"""

    def __init__(self, summaries):
        self._summaries = summaries

    def list_conversations(self):
        return list(self._summaries)

    def load_conversation(self, conv_id):
        return None

    def save_conversation(self, conv, bump_updated_at=True):
        pass


def _make_panel(summaries, **overrides):
    """无 GTK 的假 AIChatPanel：__new__ + 桩属性（test_ai_switch_unsaved_running 模式）。

    ``_history_summaries_json`` 只依赖 ``_get_sorted_conversations`` 需要的
    四个属性；其余属性置空即可。
    """
    panel = AIChatPanel.__new__(AIChatPanel)
    panel._conversation_store = _FakeConversationStore(summaries)
    panel._ai_running_convs = {}
    panel._ai_conversation_id = None
    panel._ai_messages = []
    for key, value in overrides.items():
        setattr(panel, key, value)
    return panel


class TestHistorySummariesJson(unittest.TestCase):
    """``_history_summaries_json`` 的 ts 字段 + label 格式契约。"""

    def test_each_item_has_ts_int_ms(self):
        """解析后的 JSON 每项含 ``ts`` 且为 int（毫秒数量级 > 1e12）。"""
        panel = _make_panel([
            {"id": "c1", "title": "first", "message_count": 3,
             "updated_at": 1700000000000},
            {"id": "c2", "title": "second", "message_count": 5,
             "updated_at": 1700000001000},
        ])
        items = json.loads(panel._history_summaries_json())
        self.assertEqual(len(items), 2)
        for item in items:
            self.assertIn("ts", item)
            self.assertIsInstance(item["ts"], int)
            self.assertGreater(item["ts"], 1e12)  # 毫秒数量级

    def test_ts_falls_back_to_zero_when_updated_at_missing(self):
        """缺失 ``updated_at`` 时 ts 兜底为 0（不抛 KeyError）。"""
        panel = _make_panel([
            {"id": "c1", "title": "no-updated-at", "message_count": 1},
        ])
        items = json.loads(panel._history_summaries_json())
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["ts"], 0)

    def test_items_sorted_by_ts_descending(self):
        """列表顺序按 ts 降序（新会话在前），与排序列表保持一致。"""
        panel = _make_panel([
            {"id": "old", "title": "old", "message_count": 1,
             "updated_at": 1700000000000},
            {"id": "mid", "title": "mid", "message_count": 1,
             "updated_at": 1700000002000},
            {"id": "new", "title": "new", "message_count": 1,
             "updated_at": 1700000003000},
        ])
        items = json.loads(panel._history_summaries_json())
        ids = [item["id"] for item in items]
        self.assertEqual(ids, ["new", "mid", "old"])
        ts_list = [item["ts"] for item in items]
        self.assertEqual(ts_list, sorted(ts_list, reverse=True))

    def test_label_truncation_and_count_format(self):
        """label 截断（30 字符标题 → 22 + "..."）与 ``(N条)`` 格式不回归。"""
        long_title = "abcdefghijklmnopqrstuvwxyz0123456789"  # 30 字符
        panel = _make_panel([
            {"id": "long", "title": long_title, "message_count": 7,
             "updated_at": 1700000000000},
        ])
        items = json.loads(panel._history_summaries_json())
        label = items[0]["label"]
        # title[:22] = "abcdefghijklmnopqrstuv"（22 字符）+ "..." + " (7条)"
        self.assertEqual(label, "abcdefghijklmnopqrstuv" + "..." + " (7条)")
        self.assertEqual(len(label), 22 + 3 + len(" (7条)"))

    def test_title_missing_falls_back_to_untitled(self):
        # M1：title 缺失 → 兜底生效（label 含 "untitled"，_clean_history_title
        # 会剥离 "(untitled)" 的括号 → "untitled"），不空不崩
        panel = _make_panel([
            {"id": "c1", "message_count": 1, "updated_at": 1700000000000},
        ])
        items = json.loads(panel._history_summaries_json())
        self.assertEqual(items[0]["id"], "c1")
        self.assertIn("untitled", items[0]["label"])
        self.assertNotEqual(items[0]["label"], "(1条)")

    def test_empty_store_returns_parsable_json(self):
        # M1：store 空 → 返回可解析的 "[]"
        panel = _make_panel([])
        self.assertEqual(panel._history_summaries_json(), "[]")
        self.assertEqual(json.loads(panel._history_summaries_json()), [])

    def test_store_exception_returns_empty_list(self):
        # M1：store 抛异常 → 整体兜底 "[]"（_history_summaries_json 吞异常）
        class _ExplodingStore:
            def list_conversations(self):
                raise RuntimeError("boom")

        panel = _make_panel([], _conversation_store=_ExplodingStore())
        self.assertEqual(panel._history_summaries_json(), "[]")
        self.assertEqual(json.loads(panel._history_summaries_json()), [])

    def test_bold_markdown_cleaned_from_label(self):
        # M1：title 含 **bold** → label 无 markdown 标记残留
        panel = _make_panel([
            {"id": "c1", "title": "**bold**", "message_count": 2,
             "updated_at": 1700000000000},
        ])
        items = json.loads(panel._history_summaries_json())
        self.assertEqual(items[0]["label"], "bold (2条)")
        self.assertNotIn("**", items[0]["label"])


if __name__ == "__main__":
    unittest.main()
