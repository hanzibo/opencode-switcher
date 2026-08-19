"""MCP 第三阶段（UI 设置配置解析、Master-Detail 布局与持久化验证）单元测试。"""

import unittest
from mcp_integration.server_config import MCPServerConfig
from dialogs.settings_dialog import SettingsDialog
from stores.clipboard_store import AISettingsStore


class TestMCPSettingsConfigParsing(unittest.TestCase):
    def test_env_string_parsing(self):
        env_raw = "  API_KEY = secret_123 , PORT=8080 , DEBUG = true, EMPTY_KEY= "
        env_dict = SettingsDialog._parse_env_str(env_raw)

        self.assertEqual(env_dict, {
            "API_KEY": "secret_123",
            "PORT": "8080",
            "DEBUG": "true",
            "EMPTY_KEY": "",
        })

    def test_mcp_server_config_roundtrip(self):
        raw_card_data = {
            "name": "github-mcp",
            "transport": "stdio",
            "command": "npx",
            "args": ["-y", "@modelcontextprotocol/server-github"],
            "cwd": "/home/user/repo",
            "env": {"GITHUB_PERSONAL_ACCESS_TOKEN": "ghp_123456"},
            "url": "",
            "api_key": "",
            "auth_type": "bearer",
            "oauth_client_id": "",
            "oauth_client_secret": "",
            "oauth_token_url": "",
            "oauth_scopes": "",
            "protocol_version": "2025-11-25",
            "enable_2026_headers": False,
            "enabled": True,
            "auto_connect": True,
        }

        cfg = MCPServerConfig.from_dict(raw_card_data)
        self.assertEqual(cfg.name, "github-mcp")
        self.assertEqual(cfg.cwd, "/home/user/repo")
        self.assertEqual(cfg.env, {"GITHUB_PERSONAL_ACCESS_TOKEN": "ghp_123456"})
        self.assertIsNone(cfg.validate())

        serialized = cfg.to_dict()
        self.assertEqual(serialized["cwd"], "/home/user/repo")
        self.assertEqual(serialized["env"], {"GITHUB_PERSONAL_ACCESS_TOKEN": "ghp_123456"})


class TestMCPSettingsMasterDetailUI(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from gi.repository import Gtk, Gdk
        if not Gtk.init_check()[0] or Gdk.Display.get_default() is None:
            raise unittest.SkipTest("GTK Display unavailable in this test environment")

    def setUp(self):
        self.store = AISettingsStore()
        self.store.mcp_servers = [
            {
                "name": "local-fs",
                "transport": "stdio",
                "command": "npx",
                "args": ["-y", "mcp-fs"],
                "enabled": True,
                "auto_connect": False,
            },
            {
                "name": "remote-git",
                "transport": "http",
                "url": "https://api.githubcopilot.com/mcp/",
                "enabled": False,
                "auto_connect": True,
            }
        ]

    def test_master_detail_construction(self):
        dialog = SettingsDialog(parent_window=None, ai_settings_store=self.store)

        # 检查列表行数
        self.assertEqual(len(dialog._mcp_server_widgets), 2)
        children = dialog._mcp_list_box.get_children()
        self.assertEqual(len(children), 2)

        # 检查第一项 (stdio)
        first_row = children[0]
        self.assertEqual(first_row._title_label.get_text(), "local-fs")
        self.assertIn("stdio", first_row._badge_label.get_label())

        # 检查第二项 (http)
        second_row = children[1]
        self.assertEqual(second_row._title_label.get_text(), "remote-git")
        self.assertIn("http", second_row._badge_label.get_label())

        # 检查 Stack 页面对应关系
        self.assertEqual(dialog._mcp_stack.get_visible_child_name(), first_row._server_id)

    def test_master_detail_add_and_remove(self):
        dialog = SettingsDialog(parent_window=None, ai_settings_store=self.store)

        # 添加新服务器
        dialog._add_mcp_server_card(select_new=True)
        self.assertEqual(len(dialog._mcp_server_widgets), 3)
        self.assertEqual(len(dialog._mcp_list_box.get_children()), 3)

        newest_item = dialog._mcp_server_widgets[-1]
        self.assertEqual(dialog._mcp_stack.get_visible_child_name(), newest_item["server_id"])

        # 实时修改名称联动验证
        newest_item["name"].set_text("dynamo-db")
        self.assertEqual(newest_item["row"]._title_label.get_text(), "dynamo-db")

        # 删除操作
        dialog._remove_mcp_server_card(newest_item["server_id"])
        self.assertEqual(len(dialog._mcp_server_widgets), 2)
        self.assertEqual(len(dialog._mcp_list_box.get_children()), 2)

        # 清空所有
        for w in list(dialog._mcp_server_widgets):
            dialog._remove_mcp_server_card(w["server_id"])

    def test_transport_mode_visibility_switching(self):
        dialog = SettingsDialog(parent_window=None, ai_settings_store=self.store)

        # local-fs is stdio mode
        item_stdio = dialog._mcp_server_widgets[0]
        stdio_box = item_stdio["command"].get_parent().get_parent()
        http_box = item_stdio["url"].get_parent().get_parent()

        self.assertTrue(stdio_box.get_visible())
        self.assertFalse(http_box.get_visible())

        # Calling show_all on the dialog (as done during window construction/reveal)
        # must NOT override no_show_all
        dialog._dialog.show_all()
        self.assertTrue(stdio_box.get_visible())
        self.assertFalse(http_box.get_visible())

        # Switch transport to http
        item_stdio["transport"].set_active_id("http")
        self.assertFalse(stdio_box.get_visible())
        self.assertTrue(http_box.get_visible())

        # Switch back to stdio
        item_stdio["transport"].set_active_id("stdio")
        self.assertTrue(stdio_box.get_visible())
        self.assertFalse(http_box.get_visible())


if __name__ == "__main__":
    unittest.main()

