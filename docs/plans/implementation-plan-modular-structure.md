# 实施计划：OpenCode Switcher 代码库子包模块化整理

本计划旨在将根目录下散落的 28 个 Python 代码文件，按照领域职责归纳组织到 5 个标准的 Python 包目录中（`views/`、`dialogs/`、`stores/`、`ai_engine/`、`system/`），以提升代码库的可读性、内聚性和长期可维护性。

---

## 1. 全量改动点与文件映射清单

### 1.1 目录结构变更映射

| 原文件路径 (根目录) | 新文件路径 | 属于模块包 | 核心职责 |
|---|---|---|---|
| `panel.py` | `views/panel.py` | `views` | 会话搜索与控制主面板 |
| `clipboard_panel.py` | `views/clipboard_panel.py` | `views` | 剪贴板历史与分类管理面板 |
| `ai_chat_panel.py` | `views/ai_chat_panel.py` | `views` | AI 助手对话与 WebKit 交互面板 |
| `ai_popovers.py` | `views/ai_popovers.py` | `views` | AI 操作气泡与浮动菜单组件 |
| `settings_dialog.py` | `dialogs/settings_dialog.py` | `dialogs` | 设置对话框 (LLM/MCP/Skill) |
| `prompts_config_dialog.py` | `dialogs/prompts_config_dialog.py` | `dialogs` | Prompts 模板配置对话框 |
| `memory_manager_dialog.py` | `dialogs/memory_manager_dialog.py` | `dialogs` | 长期记忆管理对话框 |
| `dynamic_copy_dialog.py` | `dialogs/dynamic_copy_dialog.py` | `dialogs` | 动态模板复制参数填写对话框 |
| `recycle_bin_dialog.py` | `dialogs/recycle_bin_dialog.py` | `dialogs` | 剪贴板回收站对话框 |
| `sort_dialog.py` | `dialogs/sort_dialog.py` | `dialogs` | 通用列表排序对话框 |
| `sort_cats_dialog.py` | `dialogs/sort_cats_dialog.py` | `dialogs` | 分类排序对话框 |
| `prompt_dialog.py` | `dialogs/prompt_dialog.py` | `dialogs` | 单个 Prompt 编辑弹窗 |
| `image_preview_dialog.py` | `dialogs/image_preview_dialog.py` | `dialogs` | 图片 Lightbox 预览弹窗 |
| `clipboard_store.py` | `stores/clipboard_store.py` | `stores` | 剪贴板、分类、记忆、Prompt 数据中心 |
| `session_store.py` | `stores/session_store.py` | `stores` | SQLite 会话读取与实时检测 |
| `skill_store.py` | `stores/skill_store.py` | `stores` | Skill 状态管理与扩展 Frontmatter 解析 |
| `theme_config.py` | `stores/theme_config.py` | `stores` | 主题配置持久化与 GTK/CSS 变量映射 |
| `llm_client.py` | `ai_engine/llm_client.py` | `ai_engine` | LLM HTTP API 异步客户端 |
| `ai_tool_loop.py` | `ai_engine/ai_tool_loop.py` | `ai_engine` | ReAct 工具调用循环与推理处理 |
| `ai_html_template.py` | `ai_engine/ai_html_template.py` | `ai_engine` | WebKit WebView HTML/JS 资源加载器 |
| `render_pipeline.py` | `ai_engine/render_pipeline.py` | `ai_engine` | 流式 Markdown & Codeblock 渲染管道 |
| `hotkey.py` | `system/hotkey.py` | `system` | Wayland Unix Socket 快捷键监听器 |
| `launcher.py` | `system/launcher.py` | `system` | PTY / GNOME Terminal 会话唤起器 |
| `event_types.py` | `system/event_types.py` | `system` | 全局事件总线与 Stream 事件定义 |
| `migrate_history.py` | `system/migrate_history.py` | `system` | 剪贴板历史数据迁移脚本 |
| `inspect_db.py` | `system/inspect_db.py` | `system` | 数据库结构调试工具 |
| `utils.py` | `system/utils.py` | `system` | 相对时间、窗口 Focus 等通用工具库 |

---

## 2. 新增的包标识文件 (5 个)

为了确保 Python 解释器能够将新目录识别为标准的 Python Package，需新建以下 `__init__.py` 文件：
1. `views/__init__.py`
2. `dialogs/__init__.py`
3. `stores/__init__.py`
4. `ai_engine/__init__.py`
5. `system/__init__.py`

---

## 3. 需更新 Import 引用的文件与关联影响

### 3.1 根目录文件
- **`main.py`**：
  - 将 `from hotkey import HotkeyManager` 更新为 `from system.hotkey import HotkeyManager`
  - 将 `from panel import SearchPanel` 更新为 `from views.panel import SearchPanel`
  - 将 `from session_store import ...` 更新为 `from stores.session_store import ...`
  - 将 `from launcher import ...` 更新为 `from system.launcher import ...`
  - 将 `from clipboard_store import ...` 更新为 `from stores.clipboard_store import ...`
  - 将 `from clipboard_panel import ClipboardPanel` 更新为 `from views.clipboard_panel import ClipboardPanel`
  - 将 `from memory_manager_dialog import ...` 更新为 `from dialogs.memory_manager_dialog import ...`
  - 将 `from theme_config import ...` 更新为 `from stores.theme_config import ...`
  - 将 `from migrate_history import ...` 更新为 `from system.migrate_history import ...`

### 3.2 工具注册表与外部模块
- **`tool_registry/` 内部各模块**（如 `bash.py`, `filesystem.py`, `subagent.py`, `memory.py`, `gmail.py`, `mail.py`）：
  - 将 `from session_store import ...` 更新为 `from stores.session_store import ...`
  - 将 `from clipboard_store import ...` 更新为 `from stores.clipboard_store import ...`
  - 将 `from skill_store import ...` 更新为 `from stores.skill_store import ...`
  - 将 `from utils import ...` 更新为 `from system.utils import ...`

### 3.3 部署脚本与规范文档
- **`install.sh`**：
  - 更新文件复制清单：复制新增的 `views/`, `dialogs/`, `stores/`, `ai_engine/`, `system/` 子目录到 `$INSTALL_DIR/`。
- **`AGENTS.md`**：
  - 更新 Module Map 模块映射表与系统架构说明。

---

## 4. 分步实施计划

### 阶段 1：基础包结构搭建 (Step 1)
- **目标**：使用 `git mv` 创建 5 个子包目录并追加 `__init__.py`。
- **改动位置**：新建 `views/`, `dialogs/`, `stores/`, `ai_engine/`, `system/`。
- **验证策略**：运行 `git status` 确保 Git 正确追踪目录创建。

### 阶段 2：数据与系统基础层迁移 (Step 2 & Step 3)
- **目标**：迁移 `stores/` (4 个文件) 与 `system/` (6 个文件)。
- **修改说明**：
  - 移动 `clipboard_store.py`, `session_store.py`, `skill_store.py`, `theme_config.py` 到 `stores/`
  - 移动 `hotkey.py`, `launcher.py`, `event_types.py`, `migrate_history.py`, `inspect_db.py`, `utils.py` 到 `system/`
  - 更新其内部交叉引用（如 `stores/skill_store.py` 引用 `stores.clipboard_store`）。

### 阶段 3：AI 引擎与对话框层迁移 (Step 4 & Step 5)
- **目标**：迁移 `ai_engine/` (4 个文件) 与 `dialogs/` (9 个文件)。
- **修改说明**：
  - 移动 `llm_client.py`, `ai_tool_loop.py`, `ai_html_template.py`, `render_pipeline.py` 到 `ai_engine/`
  - 移动 9 个 `*_dialog.py` 文件到 `dialogs/`
  - 更新内部引用路径。

### 阶段 4：UI 面板层迁移与根入口更新 (Step 6 & Step 7)
- **目标**：迁移 `views/` (4 个文件)，更新 `main.py`、`install.sh`、`tool_registry/` 与 `tests/`。
- **修改说明**：
  - 移动 `panel.py`, `clipboard_panel.py`, `ai_chat_panel.py`, `ai_popovers.py` 到 `views/`
  - 全量更新 `tool_registry/` 和 `tests/` 下的 `import` 语句。

### 阶段 5：自动化测试与运行验证 (Step 8)
- **目标**：跑通全量单元测试与运行验证。
- **测试命令**：`venv/bin/python3 -m unittest discover tests`

---

## 5. 预期风险与回退策略

- **预期风险**：部分工具模块或测试用例在延迟加载（Lazy import）时若有遗漏的 `import` 会引发 `ModuleNotFoundError`。
- **回退策略**：由于所有文件移动与修改均在独立 Git 开发分支上进行，若测试失败或出现不可预期问题，可通过 `git checkout .` 或切回 `master` 分支无损恢复。
