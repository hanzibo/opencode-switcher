"""MCP 第三阶段（UI 设置配置解析与持久化验证）单元测试。"""

import unittest
from mcp_integration.server_config import MCPServerConfig


class TestMCPSettingsConfigParsing(unittest.TestCase):
    def test_env_string_parsing(self):
        env_raw = "  API_KEY = secret_123 , PORT=8080 , DEBUG = true, EMPTY_KEY= "
        env_dict = {}
        for item in env_raw.split(","):
            if "=" in item:
                k, v = item.split("=", 1)
                if k.strip():
                    env_dict[k.strip()] = v.strip()

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


if __name__ == "__main__":
    unittest.main()
