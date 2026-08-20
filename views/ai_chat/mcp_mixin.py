"""MCP (Model Context Protocol) integration mixin for AIChatPanel."""

from typing import Optional


class MCPMixin:
    """MCP 服务器连接池管理与工具发现 Mixin。"""

    def _init_mcp(self) -> None:
        """初始化 MCP 桥接器和客户端管理器。

        应在 AI 面板首次显示或首次用户输入前调用。
        可重复调用（幂等）。
        """
        if getattr(self, "_mcp_initialized", False):
            return

        from mcp_integration import GtkAsyncioBridge, MCPClientManager, MCPServerConfig

        self._mcp_bridge = GtkAsyncioBridge.get()
        self._mcp_bridge.start()
        self._mcp_client_mgr = MCPClientManager(self._mcp_bridge)
        self._mcp_client_mgr.add_tools_changed_callback(self._on_mcp_server_tools_changed)

        # 从设置加载配置并 auto_connect
        if self._ai_settings_store is not None:
            self._load_and_connect_mcp_servers()

        self._mcp_initialized = True
        print(f"[MCP] 初始化完成，已连接 {self._mcp_client_mgr.get_server_count()} 个 Server", flush=True)

    def _on_mcp_server_tools_changed(self, server_name: str) -> None:
        """MCP Server 发出 notifications/tools/list_changed 时的通知处理。"""
        print(f"[MCP] 收到来自 {server_name} 的工具变更通知，正在刷新工具缓存...", flush=True)
        self._refresh_mcp_tools()

    def _load_and_connect_mcp_servers(self) -> None:
        """从 AISettingsStore 加载 MCP Server 配置并自动连接。"""
        if self._ai_settings_store is None or self._mcp_client_mgr is None:
            return

        from mcp_integration import MCPServerConfig
        from mcp_integration.gtk_asyncio_bridge import GtkAsyncioBridge

        server_dicts = getattr(self._ai_settings_store, "mcp_servers", None) or []
        for sd in server_dicts:
            config = MCPServerConfig.from_dict(sd)
            if not (config.enabled and config.auto_connect):
                continue
            # stdio 需要 command，http 需要 url
            if config.transport == "stdio" and not config.command:
                continue
            if config.transport == "http" and not config.url:
                continue
            if config.transport == "http":
                self._mcp_bridge.call_async(
                    self._mcp_client_mgr.connect_http(config),
                    callback=lambda result, err, n=config.name: (
                        print(f"[MCP] {n}: {result[1] if result and not err else err}", flush=True)
                        or (self._refresh_mcp_tools() if result and result[0] else None)
                    ),
                )
            else:
                self._mcp_bridge.call_async(
                    self._mcp_client_mgr.connect_stdio(config),
                    callback=lambda result, err, n=config.name: (
                        print(f"[MCP] {n}: {result[1] if result and not err else err}", flush=True)
                        or (self._refresh_mcp_tools() if result and result[0] else None)
                    ),
                )

    def _refresh_mcp_tools(self) -> None:
        """异步预取 MCP 工具列表并缓存。"""
        if self._mcp_client_mgr is None:
            return
        self._mcp_bridge.call_async(
            self._mcp_client_mgr.list_all_tools(),
            callback=self._on_mcp_tools_ready,
        )

    def _on_mcp_tools_ready(self, tools: list, err: Optional[Exception]) -> None:
        """MCP 工具列表就绪后的回调。"""
        if err:
            print(f"[MCP] 获取工具列表失败: {err}", flush=True)
            return
        if tools:
            self._cached_mcp_tools = tools
            server_count = self._mcp_client_mgr.get_server_count()
            print(
                f"[MCP] 已缓存 {len(tools)} 个工具，来自 {server_count} 个 Server",
                flush=True,
            )

    def _reconfigure_mcp(self) -> None:
        """根据配置变更重新连接/断开 MCP Server。

        在 Settings 保存后调用，使 MCP 配置即时生效而不需重启。
        """
        if not getattr(self, "_mcp_initialized", False) or self._mcp_client_mgr is None:
            return

        from mcp_integration import MCPServerConfig

        # 1. 读取新配置
        server_dicts = getattr(self._ai_settings_store, "mcp_servers", None) or []
        new_configs = {}
        for sd in server_dicts:
            config = MCPServerConfig.from_dict(sd)
            if not (config.enabled and config.auto_connect):
                continue
            # stdio 需要 command，http 需要 url
            if config.transport == "stdio" and not config.command:
                continue
            if config.transport == "http" and not config.url:
                continue
            new_configs[config.name] = config

        # 2. 断开已禁用或不再存在的 Server
        has_disconnect = False
        for name in list(self._mcp_client_mgr.get_all_server_names()):
            if name not in new_configs:
                has_disconnect = True
                self._mcp_bridge.call_async(
                    self._mcp_client_mgr.disconnect(name),
                    callback=lambda result, err, n=name: (
                        print(f"[MCP] 已断开 Server: {n}", flush=True)
                    ),
                )

        # 3. 连接新增或已启用的 Server
        has_connect = False
        for name, config in new_configs.items():
            if not self._mcp_client_mgr.is_connected(name):
                has_connect = True
                self._mcp_bridge.call_async(
                    self._mcp_client_mgr.connect_by_config(config),
                    callback=lambda result, err, n=name: (
                        print(f"[MCP] {n}: {result[1] if result and not err else err}", flush=True)
                        or (self._refresh_mcp_tools() if result and result[0] else None)
                    ),
                )

        # 4. 如果断开或改名，立即清空缓存避免使用旧工具名
        if has_disconnect and has_connect:
            self._cached_mcp_tools = None
            print("[MCP] Server 变更，清空工具缓存等待刷新", flush=True)
        elif has_disconnect and not has_connect:
            self._cached_mcp_tools = None
