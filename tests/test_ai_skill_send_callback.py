#!/usr/bin/env python3
"""AI 面板 /skill 手动触发「发送回调」RED 回归测试（fix/ai-skill-send-callback）。

已知 bug（本文件为纯测试，不修改生产代码）：
``views/ai_chat_panel.py:_handle_skill_command`` 在命名 skill 成功调取、把
``[手动触发 Skill: <name>]`` payload 写入输入框之后，调用的是**不存在**的
``self._on_ai_send_clicked()``（正确方法名为 ``_on_send_clicked``）→
``AttributeError``，payload 已就绪却永远无法发出。

覆盖：
- (a) ``/skill:<name>`` 成功路径：payload 写入输入框 + 必须触发
  ``_on_send_clicked`` 发送（当前：调用不存在的 ``_on_ai_send_clicked`` →
  AttributeError → FAIL）。
- (b) 裸 ``/skill`` 列表路径：仅渲染可用 Skill 列表，不得触发任何发送。
- (c) ``skill:name`` / ``/skill <name>`` 等别名形式同样触发发送回调。
- (d) 未知 skill 错误路径：渲染错误 HTML 并列出可用项，不得触发发送。
- (e) 守卫：payload 必须以 ``[手动触发 Skill: <name>]`` 开头并包含 Skill 内容，
  超长内容被截断（不越过 30000 上限）。

无头假面板模式：``AIChatPanel.__new__`` + 桩属性 + 假 WebView/Entry
（同 tests/test_ai_switch_back_restore.py）。SkillStore 与 bash cwd 全部 mock。
"""
import os
import unittest
from types import SimpleNamespace
from unittest import mock

os.environ.setdefault("GDK_BACKEND", "dummy")  # 无头环境导入 GTK

from views.ai_chat_panel import AIChatPanel


# ═══════════════════════════════════════════════════════════════════
#  无头假件（display-independent）
# ═══════════════════════════════════════════════════════════════════


class _FakeWebView:
    """记录 run_javascript 调用的假 WebView（同 test_ai_switch_back_restore）。"""

    def __init__(self):
        self.js_calls = []

    def run_javascript(self, js, *args):
        self.js_calls.append(js)


class _FakeBuffer:
    """记录 set_text 的假 TextBuffer（_handle_skill_command 写入 payload）。"""

    def __init__(self):
        self.text = ""

    def set_text(self, text):
        self.text = text


class _FakeEntry:
    def __init__(self):
        self._buffer = _FakeBuffer()

    def get_buffer(self):
        return self._buffer


def _make_panel(**overrides):
    """无 GTK 的假 AIChatPanel：__new__ + 桩属性（test_system_prompt 模式）。

    发送回调用 Mock 替身：修复后 ``_handle_skill_command`` 应调用
    ``_on_send_clicked``（当前因误调 ``_on_ai_send_clicked`` 而 AttributeError）。
    """
    panel = AIChatPanel.__new__(AIChatPanel)
    panel._ai_webview = _FakeWebView()
    panel._ai_entry = _FakeEntry()
    panel._ai_conversation_id = "conv1"
    panel._on_send_clicked = mock.Mock()
    for key, value in overrides.items():
        setattr(panel, key, value)
    return panel


def _fake_skill(name, description):
    return SimpleNamespace(name=name, description=description)


def _install_skill_mocks(skills, content_by_name, cwd="/tmp/proj"):
    """桩掉 _handle_skill_command 内部的 SkillStore / get_bash_cwd。

    生产代码在函数体内 ``from stores.skill_store import SkillStore`` 与
    ``from tool_registry import get_bash_cwd``，故 patch 模块属性即可生效。
    """
    store = mock.Mock()
    store.get_skills = mock.Mock(return_value=skills)
    store.get_skill_content = mock.Mock(
        side_effect=lambda name, cwd=None: content_by_name.get(name)
    )
    patch_store = mock.patch("stores.skill_store.SkillStore", return_value=store)
    patch_cwd = mock.patch("tool_registry.get_bash_cwd", return_value=cwd)
    return patch_store, patch_cwd, store


# ═══════════════════════════════════════════════════════════════════
#  (a) 成功路径：payload + 发送回调
# ═══════════════════════════════════════════════════════════════════


class TestSkillNamedTriggerSend(unittest.TestCase):
    """bug：_handle_skill_command 误调不存在的 _on_ai_send_clicked。"""

    def test_named_skill_builds_payload_and_calls_send(self):
        """/skill:<name> 成功调取后必须调用 _on_send_clicked（当前 AttributeError → FAIL）。"""
        panel = _make_panel()
        content = "# git-master\n\n使用规范...\n"
        patch_store, patch_cwd, _ = _install_skill_mocks(
            [_fake_skill("git-master", "Git 操作专家")],
            {"git-master": content},
        )
        with patch_store, patch_cwd:
            AIChatPanel._handle_skill_command(panel, "/skill:git-master")

        # payload 已写入输入框
        self.assertIn("[手动触发 Skill: git-master]", panel._ai_entry._buffer.text)
        self.assertIn(content, panel._ai_entry._buffer.text)
        # 必须触发发送回调（bug：_on_ai_send_clicked 不存在 → AttributeError）
        panel._on_send_clicked.assert_called_once()
        # 渲染了激活提示（HTML 经 json.dumps 转义，用 ASCII class 标记断言）
        js = "\n".join(panel._ai_webview.js_calls)
        self.assertIn("chat-status-notice", js)

    def test_space_alias_skill_triggers_send(self):
        """/skill <name>（空格形式）同样触发发送回调。"""
        panel = _make_panel()
        content = "# pwd-check\n\n检查路径\n"
        patch_store, patch_cwd, _ = _install_skill_mocks(
            [_fake_skill("pwd-check", "路径检查")],
            {"pwd-check": content},
        )
        with patch_store, patch_cwd:
            AIChatPanel._handle_skill_command(panel, "/skill pwd-check")

        self.assertIn("[手动触发 Skill: pwd-check]", panel._ai_entry._buffer.text)
        panel._on_send_clicked.assert_called_once()

    def test_plain_colon_alias_skill_triggers_send(self):
        """skill:<name>（无前导斜杠）同样触发发送回调。"""
        panel = _make_panel()
        content = "# notes\n\n笔记整理\n"
        patch_store, patch_cwd, _ = _install_skill_mocks(
            [_fake_skill("notes", "笔记整理")],
            {"notes": content},
        )
        with patch_store, patch_cwd:
            AIChatPanel._handle_skill_command(panel, "skill:notes")

        self.assertIn("[手动触发 Skill: notes]", panel._ai_entry._buffer.text)
        panel._on_send_clicked.assert_called_once()

    def test_oversized_skill_content_truncated(self):
        """超长 Skill 内容在写入 payload 前被截断到 30000 上限。"""
        panel = _make_panel()
        content = "x" * 60000
        patch_store, patch_cwd, _ = _install_skill_mocks(
            [_fake_skill("huge", "大内容")],
            {"huge": content},
        )
        with patch_store, patch_cwd:
            AIChatPanel._handle_skill_command(panel, "/skill:huge")

        payload = panel._ai_entry._buffer.text
        marker = "\n\n...[内容过长已自动截断]"
        prefix = "[手动触发 Skill: huge]\n\n"
        suffix = "\n\n请严格按上述 Skill 指导完成任务。"
        self.assertTrue(payload.startswith(prefix))
        self.assertTrue(payload.endswith(suffix))
        content_part = payload[len(prefix):-len(suffix)]
        # 源内容部分被截断到 30000 字符 + 实际截断标记的长度
        self.assertIn(marker, content_part)
        self.assertEqual(len(content_part), 30000 + len(marker))
        self.assertNotIn("x" * 30001, content_part, "超长内容未被截断")
        panel._on_send_clicked.assert_called_once()


# ═══════════════════════════════════════════════════════════════════
#  (b) 裸 /skill 列表路径：只渲染列表，不发送
# ═══════════════════════════════════════════════════════════════════


class TestSkillBareListing(unittest.TestCase):
    def test_bare_skill_lists_without_send(self):
        """裸 /skill 渲染可用列表，不得触发任何发送回调。"""
        panel = _make_panel()
        skills = [
            _fake_skill("git-master", "Git 操作专家"),
            _fake_skill("pwd-check", "路径检查"),
        ]
        patch_store, patch_cwd, _ = _install_skill_mocks(skills, {})
        with patch_store, patch_cwd:
            AIChatPanel._handle_skill_command(panel, "/skill")

        # HTML 经 json.dumps 转义，中文变 \uXXXX，故用 ASCII 标记断言
        js = "\n".join(panel._ai_webview.js_calls)
        self.assertIn("skill:git-master", js)
        self.assertIn("skill:pwd-check", js)
        self.assertIn("chat-model-info", js)
        # 不得触发发送，输入框不得被写入 payload
        panel._on_send_clicked.assert_not_called()
        self.assertEqual(panel._ai_entry._buffer.text, "")

    def test_bare_skill_empty_list_notice(self):
        """无可用 skill 时渲染提示 HTML，仍不得触发发送。"""
        panel = _make_panel()
        patch_store, patch_cwd, _ = _install_skill_mocks([], {})
        with patch_store, patch_cwd:
            AIChatPanel._handle_skill_command(panel, "/skill")

        js = "\n".join(panel._ai_webview.js_calls)
        self.assertIn("chat-status-notice", js)
        self.assertIn("~/.config/opencode-switcher/skills/", js)
        panel._on_send_clicked.assert_not_called()
        self.assertEqual(panel._ai_entry._buffer.text, "")


# ═══════════════════════════════════════════════════════════════════
#  (d) 未知 skill：渲染错误，不发送
# ═══════════════════════════════════════════════════════════════════


class TestSkillUnknownName(unittest.TestCase):
    def test_unknown_skill_errors_without_send(self):
        """未知 skill 渲染错误并列出可用项，不得触发发送回调。"""
        panel = _make_panel()
        skills = [_fake_skill("git-master", "Git 操作专家")]
        patch_store, patch_cwd, _ = _install_skill_mocks(skills, {})
        with patch_store, patch_cwd:
            AIChatPanel._handle_skill_command(panel, "/skill:nonexistent")

        js = "\n".join(panel._ai_webview.js_calls)
        self.assertIn("chat-system-error", js)
        self.assertIn("nonexistent", js)
        self.assertIn("git-master", js)  # 列出可用项
        panel._on_send_clicked.assert_not_called()
        self.assertEqual(panel._ai_entry._buffer.text, "")


if __name__ == "__main__":
    unittest.main()
