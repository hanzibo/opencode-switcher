# MCP 架构增强方案

## 一、背景与目标

### 现状
- `mcp_integration/` 使用纯 asyncio 实现 JSON-RPC，不依赖 MCP SDK（避免 Python 3.14 兼容问题）
- 当前仅实现 `tools/list` + `tools/call` 两个 MCP 端点
- 传输层（stdio）与协议层（JSON-RPC）耦合在同一个 `_MCPClient` 类中
- 协议版本 `2025-11-25` 硬编码，无协商机制

### 目标
1. **可维护性** — 分层解耦，每层职责单一
2. **可扩展性** — 预留多协议支持（MCP / A2A / 自定义），方便未来兼容
3. **健壮性** — 协议协商、结构化错误、超时/重连机制
4. **零 SDK 依赖** — 继续保持纯代码实现，避免第三方兼容风险

---

## 二、行业主流实现调研

| 项目 | 架构特点 | 传输层 | 协议支持 |
|------|---------|--------|---------|
| MCP Python SDK | `ClientSession` + `StdioServerParameters` | stdio / SSE / Streamable HTTP | MCP only |
| FastMCP | 装饰器风格，自动 Transport 管理 | stdio / Streamable HTTP | MCP only |
| LangChain MCP | 基于 MCP SDK 包装 | stdio | MCP only |
| Continue.dev | Transport + Session 分离 | stdio / HTTP | MCP + 自定义 |
| Cline / Boo Ai | 插件式 Transport 注册 | stdio / Streamable HTTP | MCP + A2A 规划中 |

**共识**: Transport 与 Protocol 分离是主流架构，可插拔设计是未来趋势。

---

## 三、目标架构

```
┌─────────────────────────────────────────────────────┐
│                  上层调用方                          │
│  ai_tool_loop / ai_chat_panel                       │
└─────────────────┬───────────────────────────────────┘
                  │
┌─────────────────▼───────────────────────────────────┐
│           Protocol Session Layer                    │
│  ┌─────────────────────────────────────────────┐    │
│  │ MCPSession (initialize/negotiate/send/receive)│   │
│  │ - 协议版本协商                                │    │
│  │ - Capability 交换                             │    │
│  │ - 方法路由 (tools/resources/prompts)          │    │
│  │ - 响应验证                                    │    │
│  └──────────────┬──────────────────────────────┘    │
│  ┌──────────────▼──────────────────────────────┐    │
│  │ Future: A2ASession (预留)                   │    │
│  └─────────────────────────────────────────────┘    │
└─────────────────┬───────────────────────────────────┘
                  │
┌─────────────────▼───────────────────────────────────┐
│           JSON-RPC 2.0 Layer                        │
│  JsonRpcSession                                     │
│  - Request/Response/Notification 消息模型            │
│  - ID 生成 + 挂起请求管理                            │
│  - 超时控制 + 并发限制                               │
│  - Error 标准化                                     │
└─────────────────┬───────────────────────────────────┘
                  │
┌─────────────────▼───────────────────────────────────┐
│           Transport Layer (可插拔)                   │
│  ┌──────────────┐  ┌──────────────────┐             │
│  │ StdioTransport│  │ StreamableHTTP  │  ...         │
│  │ (当前实现)    │  │ (预留)           │             │
│  └──────────────┘  └──────────────────┘             │
└─────────────────────────────────────────────────────┘
```

### 分层职责

| 层 | 类名（暂定） | 职责 | 可替换性 |
|---|-------------|------|---------|
| Transport | `BaseTransport` / `StdioTransport` | 字节流收发，子进程/HTTP 连接管理 | ✅ 可插拔 |
| JSON-RPC | `JsonRpcSession` | 消息序列化，请求/响应匹配，超时 | ✅ 协议无关 |
| Protocol | `MCPSession` | MCP 语义（init/tools/call），Capabilities | ✅ 可换其他协议 |
| Manager | `MCPClientManager`（增强） | 生命周期管理，聚合调用 | 顶层不变 |

---

## 四、分步实施计划

### Step 1 — JSON-RPC 层抽取（`json_rpc.py`，约 100 行）

从 `_MCPClient` 中提取 JSON-RPC 2.0 协议核心，使其成为独立可复用的层。

```python
# json_rpc.py — 纯数据模型 + 消息匹配
@dataclass
class JsonRpcRequest:
    id: int
    method: str
    params: dict | None

@dataclass
class JsonRpcResponse:
    id: int
    result: dict | None
    error: JsonRpcError | None

@dataclass
class JsonRpcNotification:
    method: str
    params: dict | None

@dataclass
class JsonRpcError:
    code: int
    message: str
    data: Any = None
```

```python
class JsonRpcSession:
    """基于 Transport 的 JSON-RPC 会话层。
    
    职责：
    - JSON 序列化/反序列化
    - Request ID 生成 + 挂起 Future 管理
    - 超时控制
    - 协议无关，可复用
    """
    def __init__(self, transport: BaseTransport):
        self._transport = transport
        self._req_id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._reader_task: asyncio.Task | None = None
    
    async def request(self, method: str, params: dict | None = None, timeout: float = 120) -> dict: ...
    async def notify(self, method: str, params: dict | None = None) -> None: ...
    async def connect(self): ...  # 启动 reader
    async def close(self): ...    # 取消所有 pending
```

### Step 2 — Transport 抽象（`transport.py` + `transports/`，约 80 行）

```python
# transport.py — 抽象基类
class BaseTransport(ABC):
    @abstractmethod
    async def connect(self) -> None: ...
    @abstractmethod
    async def send_line(self, data: str) -> None: ...
    @abstractmethod
    async def read_line(self) -> str | None: ...
    @abstractmethod
    async def disconnect(self) -> None: ...

# transports/stdio.py
class StdioTransport(BaseTransport):
    """子进程 stdin/stdout 传输（从 _MCPClient 提取）。"""
    def __init__(self, command: str, args: list[str], stream_limit: int = 10*1024*1024):
        self._process: asyncio.subprocess.Process | None = None
        ...

# transports/http.py (预留)
class StreamableHttpTransport(BaseTransport):
    """HTTP Streamable 传输。"""
    ...
```

### Step 3 — MCP 协议层（`mcp_session.py`，约 120 行）

```python
class MCPSession:
    """MCP 协议会话。
    
    职责：
    - 生命周期：初始化 → 操作 → 关闭
    - 协议版本协商 + Capability 交换
    - 方法路由（tools/list, tools/call, resources/list 等）
    """
    def __init__(self, json_rpc: JsonRpcSession):
        self._jrpc = json_rpc
        self.server_info: dict = {}
        self.server_capabilities: dict = {}
    
    async def initialize(self, client_info: dict, client_capabilities: dict) -> dict:
        """MCP 初始化握手，协商协议版本和能力。"""
        result = await self._jrpc.request("initialize", {
            "protocolVersion": _negotiate_version(...),
            "capabilities": client_capabilities,
            "clientInfo": client_info,
        })
        self.server_info = result.get("serverInfo", {})
        self.server_capabilities = result.get("capabilities", {})
        await self._jrpc.notify("notifications/initialized")
        return result
    
    async def list_tools(self) -> list[dict]: ...
    async def call_tool(self, name: str, arguments: dict) -> str: ...
    async def list_resources(self) -> list[dict]: ...   # 预留
    async def read_resource(self, uri: str) -> str: ...  # 预留
```

### Step 4 — 集成与兼容（改造 `client_manager.py`，约 60 行）

`MCPClientManager` 保持对外接口不变，内部使用新的分层架构：

```
旧: _MCPClient (JSON-RPC + stdio + MCP 耦合)
新: StdioTransport → JsonRpcSession → MCPSession → MCPClient
```

```python
class MCPClientManager:
    async def connect_stdio(self, config: MCPServerConfig):
        transport = StdioTransport(config.command, config.args)
        jrpc = JsonRpcSession(transport)
        session = MCPSession(jrpc)
        info = await session.initialize(...)
        self._sessions[config.name] = session
```

### Step 5 — 协议版本协商（约 15 行）

```python
_SUPPORTED_VERSIONS = ["2025-11-25", "2025-03-26"]

def _negotiate_version(client_versions: list[str], server_version: str) -> str:
    """选择双方均支持的最新版本。"""
    for v in client_versions:
        if v == server_version:
            return v
    return client_versions[0]  # 降级至客户端默认
```

### Step 6 — 增强错误处理（约 40 行）

```python
@dataclass
class ProtocolError(Exception):
    code: int
    message: str
    category: str  # "client" | "server" | "transport" | "protocol"
    retryable: bool = False
```

---

## 五、文件结构变更

```
mcp_integration/
├── __init__.py           # 导出不变，后向兼容
├── client_manager.py     # 🔧 改造：用新架构重写内部实现
├── server_config.py      # 不变
├── gtk_asyncio_bridge.py # 不变
├── tool_adapter.py       # 不变
│
├── transport.py          # 🆕 抽象基类 BaseTransport
├── transports/
│   ├── __init__.py
│   ├── stdio.py          # 🆕 StdioTransport（从 _MCPClient 提取）
│   └── http.py           # 🆕 预留 StreamableHTTP
│
├── json_rpc.py           # 🆕 JSON-RPC 2.0 会话层
├── mcp_session.py        # 🆕 MCP 协议会话层
│
└── protocols/            # 🆕 多协议预留目录
    ├── __init__.py
    └── a2a.py            # 🆕 预留 A2A 协议占位
```

---

## 六、验证方法

1. **现有功能不受影响**：
   - 连接 MCP Server（stdio）
   - 获取工具列表 → 缓存
   - 调用工具 → 结果回流 → 增量渲染
   - 断开 / 重连

2. **分层可用性**：
   - `JsonRpcSession` 可独立于 MCP 使用（单元测试）
   - 替换 `StdioTransport` 为模拟 Transport 可跑测试

3. **协议协商**：
   - Server 返回支持版本号 → 正确协商
   - Server 返回不兼容版本 → 优雅降级或报错

---

## 七、回滚思路

```bash
git revert HEAD~1  # 单次提交直接回滚
# 或
git checkout master -- mcp_integration/  # 整体还原
```

每个 Step 独立提交，可在任意 Step 后中止。

---

## 八、工作量估算

| Step | 文件 | 预计行数 | 风险 |
|------|------|---------|------|
| 1. JSON-RPC 层 | `json_rpc.py` | +100 / -0 | 低（纯提取） |
| 2. Transport 抽象 | `transport.py` + `transports/` | +110 / -0 | 低（新文件） |
| 3. MCP 协议层 | `mcp_session.py` | +130 / -0 | 低（新文件） |
| 4. 集成改造 | `client_manager.py` | +40 / -50 | 中（接口不变） |
| 5. 协议协商 | `mcp_session.py` | +15 / -0 | 低 |
| 6. 错误处理 | `json_rpc.py` + 各处 | +50 / -0 | 低 |
| **合计** | | **+445 / -50** | |

---

## 九、未来扩展预留点

```
protocols/
├── __init__.py
├── mcp.py        ← 当前实现（或从 mcp_session.py 移入）
└── a2a.py        ← Google Agent-to-Agent 协议未来支持
     │
     └─ class A2ASession:
          """A2A 协议会话（JSON-RPC based 同样可复用 JsonRpcSession）。"""
```

这种架构下，适配一个新协议只需要：
1. 实现对应 `BaseTransport`（如需新传输）
2. 实现对应 `XxxSession`（协议语义层）
3. 注册到 Manager

`JsonRpcSession` 和 `BaseTransport` 完全复用。
