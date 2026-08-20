# Settings 对话框模块化拆分实施计划 (阶段一)

本文档详细记录将单体文件 `dialogs/settings_dialog.py`（2,142 行）重构拆分为高内聚、低耦合的 `dialogs/settings/` 模块包的全面梳理与分步实施计划。

---

## 一、 改动点全量梳理 (Scope & Modifications)

### 1. 新增与修改文件清单

| 文件路径 | 类型 | 预计行数 | 职责说明 |
|:---|:---:|:---:|:---|
| [`dialogs/settings/__init__.py`](file:///home/hzb/opencode-switcher/dialogs/settings/__init__.py) | **新增** | ~20 行 | 包统一导出接口，暴露 `SettingsDialog` 与 `show_settings_dialog` |
| [`dialogs/settings/base.py`](file:///home/hzb/opencode-switcher/dialogs/settings/base.py) | **新增** | ~220 行 | 对话框主框架、CSS 样式、Notebook 选项卡调度、保存与取消事件总线 |
| [`dialogs/settings/tab_mail.py`](file:///home/hzb/opencode-switcher/dialogs/settings/tab_mail.py) | **新增** | ~270 行 | QQ 邮箱设置 Tab + Gmail OAuth 2.0 授权与状态管理 Tab |
| [`dialogs/settings/tab_ai.py`](file:///home/hzb/opencode-switcher/dialogs/settings/tab_ai.py) | **新增** | ~430 行 | AI 模型/参数配置、Skills 开关区、Tools 工具分类启用与高危禁用区 |
| [`dialogs/settings/tab_prompts.py`](file:///home/hzb/opencode-switcher/dialogs/settings/tab_prompts.py) | **新增** | ~150 行 | 系统提示词 (System Prompt) 与 润色提示词 (Polish Prompt) 标签页 |
| [`dialogs/settings/tab_streaming.py`](file:///home/hzb/opencode-switcher/dialogs/settings/tab_streaming.py) | **新增** | ~95 行 | 流式响应超时与输出控制 Tab |
| [`dialogs/settings/tab_constants.py`](file:///home/hzb/opencode-switcher/dialogs/settings/tab_constants.py) | **新增** | ~80 行 | 历史上限、FIFO 等核心常量配置 Tab |
| [`dialogs/settings/tab_theme.py`](file:///home/hzb/opencode-switcher/dialogs/settings/tab_theme.py) | **新增** | ~65 行 | 主题切换（Dark / Light / Dark-Moon）与实时广播 Tab |
| [`dialogs/settings/tab_mcp.py`](file:///home/hzb/opencode-switcher/dialogs/settings/tab_mcp.py) | **新增** | ~740 行 | MCP Master-Detail 双栏管理、表单绑定、连接测试与 OAuth 2.1 状态 |
| [`dialogs/settings_dialog.py`](file:///home/hzb/opencode-switcher/dialogs/settings_dialog.py) | **修改** | ~25 行 | 转为向后兼容门面（Facade），重定向至 `dialogs.settings` |
| [`tests/test_mcp_phase3.py`](file:///home/hzb/opencode-switcher/tests/test_mcp_phase3.py) | **验证** | 不变 | 验证原有导入与 MCP Tab 初始化兼容性 |

---

### 2. 详细改动与依赖关系梳理

#### (1) `dialogs/settings/base.py`
- **核心类**: `SettingsDialog`
- **工厂方法**: `show_settings_dialog(...)`
- **主要方法**:
  - `__init__`: 初始化窗口参数、注册 `self._tabs` 表格、初始化 Stores；
  - `build_ui`: 装配主窗口、加载 CSS Provider、创建 Notebook 容器并遍历 `self._tabs` 调用各 Tab 构建方法；
  - `_on_save`: 触发各 Tab 的收集/持久化逻辑（依次调用 `self._save_qq_mail()`, `self._save_ai_settings()`, `self._save_mcp_servers()` 等），保存后回调 `on_settings_saved`；
  - `_make_tab_scrolled_window`: 抽离通用滚动容器包装静态方法。
- **依赖关系**: 依赖 GTK3, `stores.clipboard_store` (Stores), 以及各 Tab 的构建方法与保存钩子。

#### (2) `dialogs/settings/tab_mail.py`
- **构建方法**:
  - `_build_qq_mail_tab(self)`: 渲染 QQ 邮箱授权码/密码输入框及安全提示；
  - `_build_gmail_tab(self)`: 渲染 Gmail OAuth 状态、授权/撤销按钮；
  - `_update_gmail_status_ui(self)`: 根据 Token 状态动态刷新标签文本与颜色；
  - `_on_gmail_authorize(self, btn)`, `_on_gmail_auth_done(...)`, `_on_gmail_auth_error(...)`, `_on_gmail_revoke(self, btn)`: 异步 OAuth 回调。
- **保存钩子**: `_save_qq_mail(self)`
- **依赖关系**: `stores.clipboard_store.QQMailCredentialsStore`, `GmailOAuthStore`, `tool_registry.gmail`。

#### (3) `dialogs/settings/tab_ai.py`
- **构建方法**:
  - `_build_ai_settings_tab(self)`: 渲染模型配置（模型名、上下文阈值、温度调节等）；
  - `_build_skill_toggle_section(self, parent_vbox)`: 渲染 Skill 列表启用/禁用开关；
  - `_build_tool_toggle_section(self, parent_vbox)`: 渲染 28 个工具分组折叠区及开关；
  - `_on_group_toggle(self, btn, schemas, inner_box)`: 分组折叠展开；
  - `_on_tool_enable_all(self, btn)`, `_on_tool_disable_high_risk(self, btn)`: 批量工具启停控制。
- **保存钩子**: `_save_ai_settings(self)`
- **依赖关系**: `stores.clipboard_store.AISettingsStore`, `stores.skill_store.SkillStore`, `tool_registry`。

#### (4) `dialogs/settings/tab_prompts.py`
- **构建方法**:
  - `_build_system_prompt_tab(self)`: 渲染全局系统提示词编辑区；
  - `_build_polish_prompt_tab(self)`: 渲染润色提示词编辑区、重置为默认按钮。
- **保存钩子**: `_save_prompts(self)`
- **依赖关系**: `stores.clipboard_store.AISettingsStore`。

#### (5) `dialogs/settings/tab_streaming.py`
- **构建方法**:
  - `_build_streaming_tab(self)`: 渲染首 Token 超时与停顿超时参数调节输入控件。
- **保存钩子**: `_save_streaming_settings(self)`
- **依赖关系**: `stores.clipboard_store.AISettingsStore`。

#### (6) `dialogs/settings/tab_constants.py`
- **构建方法**:
  - `_build_constants_tab(self)`: 渲染剪贴板历史上限、缓存大小等常量配置。
- **保存钩子**: `_save_constants(self)`
- **依赖关系**: `stores.clipboard_store`。

#### (7) `dialogs/settings/tab_theme.py`
- **构建方法**:
  - `_build_theme_tab(self)`: 渲染主题单选框组（Dark、Light、Dark-Moon）。
- **保存钩子**: `_save_theme(self)`（触发 `on_theme_changed` 广播）
- **依赖关系**: `stores.theme_config`。

#### (8) `dialogs/settings/tab_mcp.py`
- **构建方法**:
  - `_build_mcp_tab(self)`: 搭建左右双栏 Master-Detail 布局与操作工具栏；
  - `_create_mcp_empty_view(self)`: 无服务器时的占位空状态；
  - `_update_mcp_count_label(self)`: 刷新服务计数标签；
  - `_update_mcp_list_row(self, row, name, enabled, transport)`: 左侧列表项 Badge 与标题刷新；
  - `_on_mcp_row_selected(self, listbox, row)`: 选中行与 Stack 详情切换；
  - `_add_mcp_server_card(self, data, select_new)`: 创建并绑定右侧表单、传输类型切换、OAuth 2.1 授权状态及按钮事件；
  - `_on_test_mcp_connection(...)`: 异步测试连接并回显工具/资源列表；
  - `_remove_mcp_server_card(self, target)`: 删除服务器并切换相邻项。
- **保存钩子**: `_save_mcp_servers(self)`（导出 JSON 配置）
- **依赖关系**: `mcp_integration.MCPServerConfig`, `mcp_integration.test_server`, `mcp_integration.oauth`。

#### (9) `dialogs/settings_dialog.py` (向后兼容层)
- **代码结构**:
  ```python
  """Settings dialog backward-compatibility facade."""
  from dialogs.settings import SettingsDialog, show_settings_dialog

  __all__ = ["SettingsDialog", "show_settings_dialog"]
  ```
- **影响分析**: 确保所有原有通过 `from dialogs.settings_dialog import ...` 的调用方（`views/clipboard_panel.py`, `tests/test_mcp_phase3.py` 等）完全无感、零破坏。

---

## 二、 详细实施计划 (Step-by-Step Execution Plan)

### Step 1: 创建 `dialogs/settings/` 模块包与各 Tab 混入类 (Mixin / Tab Builder Modules)
- **目标**: 将 9 个 Tab 的 UI 构造逻辑与私有方法分别迁移至独立的子模块。
- **具体实施**:
  1. 创建目录 `dialogs/settings/`；
  2. 实现 `tab_mail.py`（封装 QQ Mail 与 Gmail Tab 逻辑）；
  3. 实现 `tab_ai.py`（封装 AI 参数、Skills 与 Tools 开关逻辑）；
  4. 实现 `tab_prompts.py`（封装系统提示词与润色提示词 Tab）；
  5. 实现 `tab_streaming.py` 与 `tab_constants.py`；
  6. 实现 `tab_theme.py`；
  7. 实现 `tab_mcp.py`（封装 MCP 双栏 Master-Detail 完整功能）。
- **风险与回退**: 各 Tab 类设计为清晰的 Mixin 或函数绑定，所有控件属性命名保持一致；若有异常可直接回退单模块代码。

### Step 2: 实现 `dialogs/settings/base.py` 主调度器与 `__init__.py`
- **目标**: 组装主对话框窗口、加载全局 CSS、通过多继承或组合调用各 Tab 的构建与保存方法。
- **具体实施**:
  1. 在 `base.py` 中定义 `SettingsDialog` 主类，集成各 Tab Mixin；
  2. 实现 `show_settings_dialog` 工厂函数；
  3. 在 `dialogs/settings/__init__.py` 统一暴露导出。
- **设计重点**: 严格维护 GTK3 对话框防重入标志 `_dialog_active` 以及 `on_dialog_shown` / `on_dialog_hidden` 触发时序。

### Step 3: 更新 `dialogs/settings_dialog.py` 门面文件
- **目标**: 将原有的 2,142 行单体文件替换为简洁的 Facade，保持 100% 向后兼容。
- **代码变动**: 仅保留对 `dialogs.settings` 的导入与 `__all__` 声明。

### Step 4: 编译检查与全量单元测试回归
- **目标**: 确保语法正确且所有 690+ 单元测试 100% 通过。
- **执行命令**:
  1. `venv/bin/python3 -m py_compile dialogs/settings/*.py dialogs/settings_dialog.py`
  2. `venv/bin/python3 -m unittest tests/test_mcp_phase3.py`
  3. `venv/bin/python3 -m unittest discover tests`

---

## 三、 收益与风险评估

- **可维护性大幅提升**: `settings_dialog.py` 单文件从 2,142 行降低至 25 行；最复杂的 MCP 面板（~740行）与 AI 设置面板（~430行）实现彻底物理隔离，修改某一功能不再需要通读数千行大文件。
- **零侵入性**: 不改变任何外部类名、函数签名与持久化 JSON 结构，测试用例无需修改。
- **GTK3 稳定性保障**: 严格遵循 `AGENTS.md` 中的 signal safety 与 dialog focus guard 规则。
