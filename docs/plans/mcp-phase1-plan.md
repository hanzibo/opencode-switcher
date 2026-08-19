# MCP 架构优化实施计划 — 第一阶段：底层 JSON-RPC 与 MCP 核心协议增强

## 1. 改动点全面梳理 (Files & Impact Analysis)

### 1.1 `mcp_integration/json_rpc.py`
- **修改类/方法**：
  - `JsonRpcSession.__init__`: 新增 `self._notification_handlers: Dict[str, List[Callable]]`，存储通知方法名到回调函数的映射。
  - `JsonRpcSession.register_notification_handler(self, method: str, handler: Callable[[Dict[str, Any]], Any]) -> None`: 新增公共方法，用于注册通知监听器（支持精确匹配和通配符如 `*`）。
  - `JsonRpcSession.unregister_notification_handler(self, method: str, handler: Callable[[Dict[str, Any]], Any]) -> None`: 新增公共方法，用于注销通知监听器。
  - `JsonRpcSession._dispatch(self, msg: Dict[str, Any]) -> None`: 修改现有逻辑，判断消息类型：
    - 若含 `"id"`：按现有流程解析 `result`/`error` 并 resolve 对应的 Future；
    - 若不含 `"id"` 且含 `"method"`：识别为 JSON-RPC Notification，分发至 `self._notification_handlers`（支持同步/异步回调），不丢弃。
  - `JsonRpcSession.request(self, method: str, params: Optional[Dict[str, Any]] = None, timeout: Optional[float] = None) -> Dict[str, Any]`:
    - 在 `asyncio.TimeoutError` 和 `asyncio.CancelledError` 异常处理分支中，向底层发送 `notifications/cancelled` 通知（携带 `requestId` 与 `reason`），实现协议级取消传播，随后再清理 pending 并向上抛出异常。
- **模块依赖与潜在影响**：
  - 作为基础通信层，改动向下兼容现有请求-响应机制；
  - 增强了取消时远端资源释放能力。

---

### 1.2 `mcp_integration/mcp_session.py`
- **修改类/方法/常量**：
  - 常量 `SUPPORTED_MCP_VERSIONS`：更新为 `["2026-07-28", "2025-11-25", "2025-03-26", "2024-11-05"]`。
  - `MCPSession.__init__`:
    - 新增 `self._on_tools_changed_callbacks: List[Callable[[], Any]]`；
    - 自动向 `_jrpc` 注册 `notifications/tools/list_changed` 回调，触发缓存清理与事件通知。
  - `MCPSession.add_tools_changed_callback(self, cb: Callable[[], Any])` / `remove_tools_changed_callback(...)`：供上层订阅工具变更事件。
  - `MCPSession._on_server_tools_list_changed(self, params: Dict[str, Any])`: 收到服务端 `notifications/tools/list_changed` 时，清空 `self._tools_cache = None` 并通知上层。
  - `MCPSession.list_tools(self, cursor: Optional[str] = None) -> List[dict]`:
    - 重构为支持游标分页的循环抓取逻辑。若响应中包含 `nextCursor`，自动循环请求直至全部工具提取完毕，并更新本地 `_tools_cache`。
  - `MCPSession.list_resources(self, cursor: Optional[str] = None) -> List[dict]`:
    - 同样重构为支持 `nextCursor` 分页读取。
  - `MCPSession.list_prompts(self, cursor: Optional[str] = None) -> List[dict]`:
    - 新增方法，支持获取 MCP Server 提供的 Prompt 列表及分页遍历。
- **模块依赖与潜在影响**：
  - 提升了对返回海量工具/分页工具的大型 MCP Server 的兼容性。

---

### 1.3 `mcp_integration/client_manager.py`
- **修改类/方法**：
  - `MCPClientManager.__init__`: 新增 `self._tools_changed_callbacks: List[Callable[[str], None]]`。
  - `MCPClientManager.add_tools_changed_callback(self, cb: Callable[[str], None])` / `remove_tools_changed_callback(...)`: 允许外部（如 UI 面板）监听任意已连接 Server 的工具变动。
  - `connect_stdio` / `connect_http`: 在 session 初始化成功后，绑定 `session.add_tools_changed_callback(lambda: self._on_session_tools_changed(config.name))`。
  - `_on_session_tools_changed(self, server_name: str)`: 广播工具变动事件给所有注册的监听器。
- **模块依赖与潜在影响**：
  - 连接管理器在工具热变更时能够主动向外推送事件。

---

### 1.4 `views/ai_chat_panel.py`
- **修改类/方法**：
  - `_init_mcp(self)`: 在创建 `self._mcp_client_mgr` 后，调用 `self._mcp_client_mgr.add_tools_changed_callback(self._on_mcp_server_tools_changed)`。
  - 新增 `_on_mcp_server_tools_changed(self, server_name: str)`: 当收到工具热更新通知时，通过 GTK 桥接异步调用 `_refresh_mcp_tools()` 刷新缓存，确保正在运行的会话和下一次对话立即获得最新工具定义。
- **模块依赖与潜在影响**：
  - 保持 AI 面板中的 MCP 工具集与服务端实时同步。

---

### 1.5 单元测试 `tests/test_mcp_phase1.py`
- **新增测试用例**：
  - `test_notification_registration_and_dispatch`: 验证服务端通知正确分发至注册的 handler。
  - `test_tool_list_cursor_pagination`: 验证 `tools/list` 在返回多页 `nextCursor` 时的全量组装。
  - `test_cancel_notification_sent_on_timeout_and_cancel`: 验证请求超时或被取消时向服务端发出 `notifications/cancelled`。
  - `test_dynamic_tools_list_changed_trigger`: 验证收到 `notifications/tools/list_changed` 时缓存清空及上层回调触发。

---

## 2. 分步骤实施计划 (Step-by-Step Execution Plan)

### Step 1: 升级 `json_rpc.py`（通知路由与取消传播）
- **目标**：实现 Notification 注册分发机制与请求取消协议级通知。
- **涉及文件**：`mcp_integration/json_rpc.py`
- **改动说明**：
  1. 在 `JsonRpcSession` 维护 `_notification_handlers` 字典；
  2. 在 `_dispatch` 中区分带 `id` 消息与通知消息；
  3. 在 `request` 的 `except (asyncio.TimeoutError, asyncio.CancelledError)` 分支中异步发送 `notifications/cancelled`。
- **回退策略**：若抛出通知发送失败异常，确保捕获并不掩盖原始的 Timeout/Cancelled 错误。

### Step 2: 增强 `mcp_session.py`（协议版本、分页、工具热更）
- **目标**：完善版本协商列表，实现游标分页与 `list_changed` 事件处理。
- **涉及文件**：`mcp_integration/mcp_session.py`
- **改动说明**：
  1. 扩充 `SUPPORTED_MCP_VERSIONS`；
  2. 实现 `list_tools` 和 `list_resources` 的 `nextCursor` 循环抓取；
  3. 添加 `list_prompts` 基础支持；
  4. 订阅 `notifications/tools/list_changed`。

### Step 3: 连接管理器与 UI 联动 (`client_manager.py` & `ai_chat_panel.py`)
- **目标**：打通动态工具更新链路至 AI 聊天面板。
- **涉及文件**：`mcp_integration/client_manager.py`、`views/ai_chat_panel.py`
- **改动说明**：
  1. 在 `client_manager.py` 转发各 Session 的工具变动事件；
  2. 在 `ai_chat_panel.py` 监听工具变动并自动触发 `_refresh_mcp_tools()`。

### Step 4: 编写并运行单元测试
- **目标**：编写 `tests/test_mcp_phase1.py`，并运行全量测试套件（`venv/bin/python3 -m unittest discover tests`）。
- **验证标准**：所有测试通过，无 regression，测试用例覆盖全部新功能。
