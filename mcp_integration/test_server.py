"""MCP 集成测试工具 — 简易测试 Server + 连接验证。

用法 1：启动测试 Server（被 MCP 客户端连接）
    python3 -m mcp_integration.test_server

用法 2：运行连接测试
    python3 -m mcp_integration.test_server --test

用法 3：生成 ai_settings.json 配置示例
    python3 -m mcp_integration.test_server --config
"""

from __future__ import annotations

import sys
import json
import asyncio
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# ═══════════════════════════════════════════════════════════════════
#  简易测试 MCP Server（Echo Server）
# ═══════════════════════════════════════════════════════════════════

def run_test_server():
    """启动一个标准 I/O 的 MCP 测试服务器（echo server）。

    这个服务器实现了一个简单的 MCP 协议握手，提供一个 echo 工具。
    """
    import json as _json
    import sys as _sys

    def _read():
        line = _sys.stdin.readline()
        if not line:
            return None
        return _json.loads(line.strip())

    def _write(obj):
        data = _json.dumps(obj, ensure_ascii=False)
        _sys.stdout.write(data + "\n")
        _sys.stdout.flush()

    initialized = False

    while True:
        msg = _read()
        if msg is None:
            break

        method = msg.get("method", "")
        msg_id = msg.get("id")
        err_ctx = msg_id  # for exception handler

        try:
            if method == "initialize":
                _write({
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "result": {
                        "protocolVersion": "2025-11-25",
                        "capabilities": {"tools": {}},
                        "serverInfo": {
                            "name": "mcp-test-server",
                            "version": "1.0.0"
                        }
                    }
                })

            elif method == "notifications/initialized":
                initialized = True

            elif method == "ping":
                if msg_id is not None:
                    _write({"jsonrpc": "2.0", "id": msg_id, "result": {}})

            elif method == "tools/list":
                if not initialized:
                    continue
                _write({
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "result": {
                        "tools": [
                            {
                                "name": "echo",
                                "description": "回显输入内容（测试工具）",
                                "inputSchema": {
                                    "type": "object",
                                    "properties": {
                                        "message": {
                                            "type": "string",
                                            "description": "要回显的消息"
                                        }
                                    },
                                    "required": ["message"]
                                }
                            },
                            {
                                "name": "add",
                                "description": "两数相加",
                                "inputSchema": {
                                    "type": "object",
                                    "properties": {
                                        "a": {"type": "number", "description": "加数"},
                                        "b": {"type": "number", "description": "被加数"}
                                    },
                                    "required": ["a", "b"]
                                }
                            }
                        ]
                    }
                })

            elif method == "tools/call":
                tool_name = msg.get("params", {}).get("name", "")
                arguments = msg.get("params", {}).get("arguments", {})

                if tool_name == "echo":
                    text = arguments.get("message", "")
                    _write({
                        "jsonrpc": "2.0",
                        "id": msg_id,
                        "result": {
                            "content": [
                                {"type": "text", "text": f"ECHO: {text}"}
                            ]
                        }
                    })
                elif tool_name == "add":
                    a = arguments.get("a", 0)
                    b = arguments.get("b", 0)
                    _write({
                        "jsonrpc": "2.0",
                        "id": msg_id,
                        "result": {
                            "content": [
                                {"type": "text", "text": f"{a} + {b} = {a + b}"}
                            ]
                        }
                    })
                else:
                    _write({
                        "jsonrpc": "2.0",
                        "id": msg_id,
                        "error": {"code": -32601, "message": f"未知工具: {tool_name}"}
                    })

        except (EOFError, BrokenPipeError):
            break
        except Exception as e:
            try:
                if err_ctx:
                    _write({
                        "jsonrpc": "2.0",
                        "id": err_ctx,
                        "error": {"code": -32603, "message": str(e)}
                    })
            except Exception:
                break


# ═══════════════════════════════════════════════════════════════════
#  连接测试（使用 asyncio 直接运行，不依赖 GLib）
# ═══════════════════════════════════════════════════════════════════

async def test_connection():
    """测试 MCP 客户端管理器能否连接到测试 Server。"""
    from mcp_integration import GtkAsyncioBridge, MCPClientManager, MCPServerConfig

    bridge = GtkAsyncioBridge.get()
    bridge.start()

    mgr = MCPClientManager(bridge)

    config = MCPServerConfig(
        name="test-server",
        command=sys.executable,
        args=["-m", "mcp_integration.test_server"],
        auto_connect=True,
    )

    print(f"🔄 连接测试 Server: {config.command} {' '.join(config.args)}")
    ok, msg = await mgr.connect_stdio(config)
    print(f"  → {msg}")

    if ok:
        tools = await mgr.list_tools("test-server")
        print(f"\n📋 可用工具 ({len(tools)}):")
        for t in tools:
            print(f"   • {t.name}: {t.description}")

        print("\n🛠️  调用 echo...")
        result = await mgr.call_tool("test-server", "echo", {"message": "Hello MCP!"})
        print(f"   → {result}")

        print("\n🛠️  调用 add(3, 5)...")
        result = await mgr.call_tool("test-server", "add", {"a": 3, "b": 5})
        print(f"   → {result}")

        await mgr.disconnect("test-server")
        print(f"\n🔌 已断开")

    bridge.stop()
    print("\n✅ 测试完成!")


# ═══════════════════════════════════════════════════════════════════
#  生成 ai_settings.json 配置示例
# ═══════════════════════════════════════════════════════════════════

def show_config_examples():
    """打印 MCP Server 配置示例（可复制到 ai_settings.json）。"""
    python_path = sys.executable

    examples = {
        "mcp_servers": [
            {
                "name": "test-server",
                "transport": "stdio",
                "command": python_path,
                "args": ["-m", "mcp_integration.test_server"],
                "enabled": True,
                "auto_connect": True,
            },
            {
                "name": "filesystem",
                "transport": "stdio",
                "command": "npx",
                "args": [
                    "-y",
                    "@modelcontextprotocol/server-filesystem",
                    "/tmp",
                    "/home/hzb",
                ],
                "enabled": False,
                "auto_connect": False,
            },
            {
                "name": "fetch",
                "transport": "stdio",
                "command": "uvx",
                "args": ["mcp-server-fetch"],
                "enabled": False,
                "auto_connect": False,
            },
            {
                "name": "git",
                "transport": "stdio",
                "command": "uvx",
                "args": ["mcp-server-git", "--repository", "."],
                "enabled": False,
                "auto_connect": False,
            },
        ]
    }

    print("=" * 60)
    print("📋 MCP Server 配置示例")
    print("=" * 60)
    print()
    print(json.dumps(examples, indent=2, ensure_ascii=False))
    print()
    print("=" * 60)
    print("💡 将以上 mcp_servers 数组添加到 ai_settings.json")
    print("   路径: ~/.config/opencode-switcher/ai_settings.json")
    print("   注意: 确保 version 字段 >= 4")
    print("=" * 60)


# ═══════════════════════════════════════════════════════════════════
#  入口
# ═══════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    if "--test" in sys.argv:
        asyncio.run(test_connection())
    elif "--config" in sys.argv:
        show_config_examples()
    else:
        # 注意：MCP 协议使用 stdout 通信，不能输出任何非 JSON 内容
        run_test_server()
