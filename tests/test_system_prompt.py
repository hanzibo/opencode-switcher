"""系统提示词功能测试：数据层持久化 + 请求层注入语义。"""
import json
import os
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault("GDK_BACKEND", "dummy")  # 无头环境导入 GTK

from stores import clipboard_store as cs
from stores.clipboard_store import AISettingsStore
from views.ai_chat_panel import AIChatPanel


class TestSystemPromptStore(unittest.TestCase):
    """AISettingsStore.system_prompt 持久化 round-trip 与旧文件兼容。"""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.patchers = [
            patch.object(cs, "CONFIG_DIR", self.temp_dir.name),
            patch.object(cs, "AI_SETTINGS_PATH",
                         os.path.join(self.temp_dir.name, "ai_settings.json")),
        ]
        for p in self.patchers:
            p.start()
        self.addCleanup(self._stop_patchers)

    def _stop_patchers(self):
        for p in self.patchers:
            p.stop()
        self.temp_dir.cleanup()

    def test_round_trip(self):
        store = AISettingsStore()
        self.assertEqual(store.system_prompt, "")
        store.system_prompt = "你是一个资深 Python 工程师"
        store.save()

        store2 = AISettingsStore()
        self.assertEqual(store2.system_prompt, "你是一个资深 Python 工程师")

    def test_legacy_file_without_system_prompt(self):
        # 旧版配置（无 system_prompt 键）应兼容为空
        with open(cs.AI_SETTINGS_PATH, "w") as f:
            json.dump({"version": 4, "soft_limit": 200}, f)
        store = AISettingsStore()
        self.assertEqual(store.system_prompt, "")


class TestSystemPromptInjection(unittest.TestCase):
    """_build_llm_messages 的 system prompt 注入语义（不污染 _ai_messages）。"""

    def _make_panel(self):
        panel = AIChatPanel.__new__(AIChatPanel)
        panel._ai_system_prompt = ""
        panel._ai_summary = ""
        panel._ai_messages = []
        return panel

    def test_system_prompt_first_in_extra(self):
        panel = self._make_panel()
        panel._ai_system_prompt = "你是资深工程师"
        panel._ai_summary = "早期讨论了 X"
        panel._ai_messages = [{"role": "user", "content": "你好"}]

        msgs, extra = panel._build_llm_messages()
        self.assertEqual(msgs, [{"role": "user", "content": "你好"}])
        self.assertEqual(len(extra), 2)
        self.assertEqual(extra[0], {"role": "system", "content": "你是资深工程师"})
        self.assertTrue(extra[1]["content"].startswith("【历史摘要】"))

    def test_empty_system_prompt_not_injected(self):
        panel = self._make_panel()
        panel._ai_system_prompt = ""
        panel._ai_summary = "有摘要"
        msgs, extra = panel._build_llm_messages()
        self.assertEqual(len(extra), 1)
        self.assertTrue(extra[0]["content"].startswith("【历史摘要】"))

    def test_all_empty_extra(self):
        panel = self._make_panel()
        msgs, extra = panel._build_llm_messages()
        self.assertEqual(extra, [])


if __name__ == "__main__":
    unittest.main()
