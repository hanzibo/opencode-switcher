# MCP 架构优化实施计划 — 第二阶段：Transport 传输层与配置模型强化

## 1. 改动点全面梳理 (Files & Impact Analysis)

### 1.1 `mcp_integration/server_config.py`
- **修改类/方法**：
  - `MCPServerConfig`: 新增属性 `env: Dict[str, str] = field(default_factory=dict)`，用于配置 MCP Server 子进程的环境变量（如 API Token、PATH 等）。
  - `MCPServerConfig.to_dict`: 序列化字典增加 `"env": dict(self.env)`。
  - `MCPServerConfig.from_dict`: `valid_keys` 增加 `"env"`，向后兼容处理 `filtered.setdefault("env", {})`。
  - `MCPServerConfig.validate`: 增加针对 `env`（必须为字典且键值均为字符串）与 `cwd`（若存在必须为非空字符串）的合法性校验。
- **模块依赖与潜在影响**：
  - 数据模型变更完全向后兼容，旧版配置读取时自动填充空字典。

---

### 1.2 `mcp_integration/transports/stdio.py`
- **修改类/方法**：
  - `StdioTransport.__init__`:
    - 参数签名扩展为 `def __init__(self, command: str, args: List[str], cwd: Optional[str] = None, env: Optional[Dict[str, str]] = None) -> None:`
    - 记录 `self._cwd = os.path.abspath(os.path.expanduser(cwd)) if cwd else None`
    - 记录 `self._env = dict(env) if env else None`
  - `StdioTransport.connect`:
    - 合并当前进程环境变量 `os.environ.copy()` 与自定义 `self._env`；
    - 在调用 `asyncio.create_subprocess_exec` 时传入 `cwd=self._cwd` 与 `env=merged_env`。
- **模块依赖与潜在影响**：
  - 使 Stdio 模式能够真正运行在指定的工作目录下，并携带用户指定的独立环境变量。

---

### 1.3 `mcp_integration/client_manager.py`
- **修改类/方法**：
  - `MCPClientManager.connect_stdio(self, config: MCPServerConfig)`:
    - 构造 `StdioTransport` 时，将 `cwd=config.cwd, env=config.env` 传递给底层 Transport。
- **模块依赖与潜在影响**：
  - 打通配置对象到底层传输的属性传递。

---

### 1.4 单元测试 `tests/test_mcp_stdio.py`
- **新增测试用例**：
  - `test_stdio_cwd_and_env_passed_to_subprocess`: 启动 Python 子进程打印 `os.getcwd()` 和 `os.environ.get('CUSTOM_VAR')`，验证 `cwd` 与自定义 `env` 环境变量均生效。
  - `test_server_config_env_serialization_and_validation`: 验证 `MCPServerConfig` 的序列化、反序列化及校验逻辑。

---

## 2. 分步骤实施计划 (Step-by-Step Execution Plan)

### Step 1: 升级配置模型 (`server_config.py`)
- **目标**：在 `MCPServerConfig` 中支持 `env` 环境变量字典及其序列化/校验。
- **涉及代码**：`mcp_integration/server_config.py`
- **改动说明**：增加字段、更新 `to_dict`、`from_dict` 与 `validate`。

### Step 2: 升级 Stdio 传输层 (`stdio.py`)
- **目标**：在 `StdioTransport` 中合并环境变量并传递 `cwd` / `env` 给 `create_subprocess_exec`。
- **涉及代码**：`mcp_integration/transports/stdio.py`
- **改动说明**：初始化保存 `cwd`/`env`，在 `connect()` 中合并环境变量并传入子进程创建调用。

### Step 3: 更新连接管理器 (`client_manager.py`)
- **目标**：`connect_stdio` 构造 `StdioTransport` 时传入 `config.cwd` 与 `config.env`。
- **涉及代码**：`mcp_integration/client_manager.py:87`

### Step 4: 编写并运行单元测试
- **目标**：在 `tests/test_mcp_stdio.py` 中增加对 `cwd` 与 `env` 传递的测试，并运行全量测试套件验证。
- **验证标准**：所有测试通过，无 regression。
