# MCP OAuth 2.1 认证能力补全 — 调研结论与实施计划

> **分支名**：`fix/mcp-oauth2`
> **日期**：2026-08-12
> **关键词**：`MCP` `OAuth 2.1` `RFC 9728` `PKCE` `RFC 8414` `RFC 8707` `RFC 7591` `Streamable HTTP` `认证`
> **测试目标**：`https://mcp.smithery.ai/jibo96701436`（已实测，完整支持现代 OAuth 栈）

---

## 1. 需求摘要 (Requirement Summary)

当前 `mcp_integration/` 只有「静态 Bearer」一条认证路径。`MCPServerConfig` 中的 `auth_type="oauth2"` 及 `oauth_client_id/secret/token_url` 字段为「预留」占位，**全链路零实现**：遇到需要 OAuth 的远程 MCP Server（如 smithery、无 PAT 的 GitHub Copilot）时，401 后直接报「连接失败」，无法自动完成认证。

本计划依据 MCP 2025-11-25 授权规范（OAuth 2.1 子集）补全：

1. 授权服务器发现（PRM RFC 9728 + AS 元数据 RFC 8414 / OIDC）
2. 客户端注册（DCR RFC 7591 / 预注册 / 用户输入兜底）
3. PKCE S256 授权码流（loopback 回调 + 系统浏览器）
4. Token 持久化（0o600）与刷新
5. 401/403 自动触发（重新）认证，Step-Up Scope 升级

---

## 2. 调研结论 (Research Findings)

来源（2026-08-12 实抓）：
- [MCP 2025-11-25 Authorization 规范](https://modelcontextprotocol.io/specification/2025-11-25/basic/authorization)
- [RFC 9728 OAuth 2.0 Protected Resource Metadata](https://www.rfc-editor.org/rfc/rfc9728.txt)
- [What's New In The 2025-11-25 MCP Authorization Spec (den.dev)](https://den.dev/blog/mcp-november-authorization-spec/)
- 官方 MCP Python SDK `mcp/client/auth/oauth2.py`（算法参考；依赖 anyio+httpx，Py3.14 挂死，**仅借鉴思路，不可直接复用**）

### 2.1 授权服务器发现（MUST）

- MCP Server **MUST** 实现 RFC 9728 PRM；客户端 **MUST** 用 PRM 发现 AS。
- 客户端 **MUST** 支持两种 PRM 发现，优先级：
  1. `WWW-Authenticate: Bearer ... resource_metadata="<URL>"`（RFC 9728 §5.1）
  2. Well-Known 回退（RFC 9728 §3.1 路径插入）：
     - `https://host/.well-known/oauth-protected-resource/<path>`（有路径时）
     - `https://host/.well-known/oauth-protected-resource`（根）
- PRM 文档校验：`resource` 字段必须与请求 URI 一致，否则**不得使用**（防冒充）。
- AS 元数据发现（客户端 MUST 支持 RFC 8414 + OIDC）：
  - 无路径 issuer：`/.well-known/oauth-authorization-server` → `/.well-known/openid-configuration`
  - 有路径 issuer：`/.well-known/oauth-authorization-server/<path>` → `/.well-known/openid-configuration/<path>` → `<path>/.well-known/openid-configuration`

### 2.2 客户端注册（优先级）

1. 预注册 client_id（有则直接用）
2. CIMD（AS 元数据 `client_id_metadata_document_supported: true` 时，SHOULD）
3. DCR（AS 元数据有 `registration_endpoint` 时，RFC 7591）
4. 兜底：UI 让用户输入 client_id/secret

### 2.3 PKCE 授权码流（MUST）

- **PKCE 强制**，`code_challenge_method=S256`；先校验 AS 元数据 `code_challenge_methods_supported` 含 `S256`。
- `resource` 参数（RFC 8707）**MUST 同时**出现在授权请求与 token 请求中，值为 MCP Server 规范 URI（无 fragment、无尾斜杠、小写 scheme/host）。
- 重定向 URI：`http://127.0.0.1:<随机端口>/callback`（loopback）；必须带 `state` 防 CSRF。

### 2.4 Token 使用 / 持久化 / 刷新

- 每个请求都必须带 `Authorization: Bearer <token>`；禁止放 query string。
- 刷新：`grant_type=refresh_token`（AS 元数据含 `refresh_token` 时）。
- 401 = token 无效/过期 → 先尝试刷新，刷新失败重走授权。

### 2.5 401/403 自动触发重认证

- 客户端 **MUST** 解析 `WWW-Authenticate` 并对 401 做出响应（自动触发授权流后重试，限 1–2 次）。
- 403 `error="insufficient_scope"` → **Step-Up Authorization Flow**：解析所需 scope（challenge 的 `scope` 优先，否则 PRM `scopes_supported`）→ 重新授权 → 重试（限次，防死循环）。
- 状态码语义：401=需要认证/token 无效；403=scope 不足；400=授权请求格式错误。

### 2.6 测试目标实测（smithery）

| 步骤 | 实测结果 |
|---|---|
| 无认证 POST `https://mcp.smithery.ai/jibo96701436` | `401` + `WWW-Authenticate: Bearer error="invalid_token", resource_metadata="https://mcp.smithery.ai/.well-known/oauth-protected-resource/jibo96701436", scope="connections:execute"` |
| PRM | `{"resource":"https://mcp.smithery.ai/jibo96701436","authorization_servers":["https://connect-auth.smithery.ai"],"scopes_supported":["connections:execute"]}` |
| AS 元数据 | `authorization_endpoint=/authorize`、`token_endpoint=/token`、`code_challenge_methods_supported:["S256"]`、`token_endpoint_auth_methods_supported:["none"]`、`grant_types_supported:["authorization_code","refresh_token"]`、`registration_endpoint=/register`、`client_id_metadata_document_supported:true` |

结论：smithery 是 ideal 端到端目标——public client（无 secret）+ DCR + PKCE S256 + refresh token 全覆盖，无需预注册。

---

## 3. 当前代码与官方实现差距对比 (Gap Analysis)

| 能力 | 官方要求 | 当前代码 | 差距 |
|---|---|---|---|
| `WWW-Authenticate` 解析 | MUST | `transports/http.py` 401/403 只读 body，header 未解析 | ❌ |
| PRM 发现（header + well-known） | MUST | 无 | ❌ |
| AS 元数据发现（RFC 8414 + OIDC） | MUST | 无 | ❌ |
| 客户端注册（DCR/CIMD/预注册） | 3 选 1 | `server_config.py` oauth 字段「预留」，零消费方 | ❌ |
| PKCE S256 授权码流 | MUST | 无 | ❌ |
| loopback 回调 + 系统浏览器 | MUST | 无 | ❌ |
| `resource` 参数（RFC 8707） | MUST | 无 | ❌ |
| Bearer 注入 | 动态 token | 仅静态 `api_key` | ⚠️ |
| Token 持久化 / 刷新 | 必须 | 无 | ❌ |
| 401 自动触发授权 | MUST | 抛 `HttpTransportAuthError` 后死路 | ❌ |
| 403 insufficient_scope 升级授权 | SHOULD | 无 | ❌ |
| UI（OAuth 配置/授权按钮/状态） | 可配置 | 「OAuth 2.1」下拉是空壳 | ❌ |

---

## 4. 修复方案 (Fix Design)

### 4.1 新增模块：`mcp_integration/oauth/`（纯 asyncio + aiohttp，避开 anyio 兼容问题）

| 文件 | 职责 |
|---|---|
| `models.py` | 数据类：`ProtectedResourceMetadata`、`OAuthMetadata`（AS 元数据）、`OAuthToken`、`PKCEParameters`、`ClientRegistrationRequest`、`OAuthClientInformationFull` |
| `discovery.py` | `parse_www_authenticate()`（提取 resource_metadata/scope/error）；`discover_protected_resource_metadata()`（header 优先，well-known 回退，校验 resource）；`discover_oauth_metadata()`（RFC 8414 → OIDC 回退，含路径插入规则） |
| `registration.py` | `dynamic_register()`（POST registration_endpoint，RFC 7591）；预注册 client_id 直通 |
| `flow.py` | `PKCEParameters.generate()`（S256）；`start_redirect_server()`（asyncio loopback）；`open_browser()`（xdg-open）；`build_authorization_url()`；`exchange_code()`；`refresh_token()` |
| `token_store.py` | `~/.config/opencode-switcher/mcp_oauth/<server>.json`（0o600），load/save/clear |
| `provider.py` | `OAuth2AuthProvider`：`get_access_token()`（有效→直接用；过期→刷新；刷新失败→重走授权）；实现 `AuthProvider` 接口 |

### 4.2 修改现有模块

| 文件 | 改动 |
|---|---|
| `mcp_integration/transports/http.py` | ① 抽象 `AuthProvider` 接口（`apply_headers` / `handle_challenge`）；② `__init__` 接收 `auth_provider`（api_key 静态路径包装为 `StaticBearerAuthProvider`）；③ 401/403 解析 `WWW-Authenticate`，OAuth challenge → 自动授权 → 重试原请求（限 1–2 次）；④ 每请求注入动态 Bearer |
| `mcp_integration/client_manager.py` | `connect_http()` 按 `config.auth_type` 构建 provider（oauth2 → OAuth2AuthProvider；bearer/none → 静态），真正消费 `oauth_*` 字段 |
| `mcp_integration/server_config.py` | 增加 `oauth_scopes` 字段（可选）；`auth_type` 校验 |
| `dialogs/settings_dialog.py` | 选「OAuth 2.1」显示：client_id（可选）、scopes（可选）、「开始授权」按钮、授权状态（未授权/已授权/已过期）；测试连接与保存支持 oauth2 |

### 4.3 关键实现步骤（实施顺序）

1. `models.py` + `discovery.py` + 解析/发现单测（用 smithery/github 真实 header 样本）
2. `token_store.py` + 持久化单测（含权限位）
3. `flow.py`：PKCE → loopback 回调 → 浏览器 → token 交换 → 刷新（跑在 `GtkAsyncioBridge` 线程）
4. `provider.py` + `http.py` 改造（AuthProvider 注入 + 401 自动触发 + 重试）
5. `client_manager.py` 接线 + `server_config.py` 字段
6. UI（授权按钮、状态展示、保存）

### 4.4 测试方式

1. **单元测试**（`tests/test_mcp_oauth.py`，headless）：WWW-Authenticate 解析（smithery/github 真实样本）、well-known URL 构造（路径插入）、PKCE 生成、token 序列化、`resource` 参数拼接。
2. **Mock OAuth 服务器**（`tests/mock_oauth_server.py`，aiohttp）：401 challenge + PRM + AS 元数据 + `/authorize` 自动回跳 + `/token`（code/refresh）+ `/register`，全自动验证：401 → 发现 → 注册 → PKCE → 换 token → 带 Bearer 重试 → 工具列表可用 → 过期自动刷新 → 刷新失败重走授权。
3. **smithery 实连集成测试**（手动/标记跳过）：连 `https://mcp.smithery.ai/jibo96701436` → 弹浏览器 → 授权 → 缓存 token → 二次启动免登录 → 验证 refresh 路径。

---

## 5. 参考实现对照

官方 MCP Python SDK（venv 已装）`mcp/client/auth/oauth2.py` 提供完整算法结构：
- `PKCEParameters.generate()`：128 位 verifier + SHA256 → urlsafe_b64 去 padding
- `OAuthContext`：token 有效性/可刷新判断、`resource` URL 计算（RFC 8707）、`prepare_token_auth`（none/basic/post）
- `OAuthClientProvider(httpx.Auth)`：401 触发流、自动注册、token 存储

**约束**：SDK 依赖 anyio + httpx，Python 3.14 下 anyio.open_process 挂死（本项目已弃用 SDK 传输层）。本实现必须用 **asyncio + aiohttp** 重写等价逻辑，与现有 `HttpTransport` 的 aiohttp 技术栈一致。

---

## 6. 验收标准 (Acceptance Criteria)

- [ ] `tests/test_mcp_oauth.py` 全部通过（解析/发现/PKCE/token_store/flow）
- [ ] Mock OAuth 服务器集成测试通过（401 → 自动认证 → 重试成功 → 刷新）
- [ ] `client_manager.connect_http` 对 `auth_type="oauth2"` 配置可端到端自动认证（smithery 实连）
- [ ] 既有 MCP 测试（15 用例）不回归
- [ ] 静态 Bearer 路径行为不变（向后兼容）
