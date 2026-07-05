# Agent 工具扩展规划

当前 `tool_registry.py` 已实现 **11 个工具**。本文档对比主流 coding agent（Claude Code、Cursor、GitHub Copilot）的工具能力，梳理差距并按优先级规划扩展方向。

> 本文档基于 2026 年 7 月主流产品的公开文档整理。

---

## 当前工具清单（11 个）

| # | 工具 | 层级 | 说明 |
|---|------|------|------|
| 1 | `web_search` | 网络 | 通过 Obscura 搜索引擎搜索互联网 |
| 2 | `web_fetch` | 网络 | 获取指定 URL 的页面内容 |
| 3 | `list_directory` | 文件 | 列出目录内容 |
| 4 | `read_file` | 文件 | 读取文件（支持行范围） |
| 5 | `grep_search` | 搜索 | 按正则搜索文件内容 |
| 6 | `glob_find` | 搜索 | 按文件名模式搜索 |
| 7 | `file_info` | 文件 | 获取文件/目录元信息 |
| 8 | `ask_user_question` | 交互 | 向用户提问 |
| 9 | `write_file` | 文件 | 创建/覆盖文件 |
| 10 | `bash` | 执行 | 持久 bash 会话（支持 restart/timeout） |
| 11 | `get_current_time` | 工具 | 获取当前时间 |

---

## 主流产品工具能力参考

### Claude Code（最完整梯队）

| 类别 | 工具 | 作用 |
|------|------|------|
| **文件** | `Read`, `Write`, `Edit`, `Glob`, `Grep` | 基础读写搜索 |
| **代码智能** | `LSP` | 跳转定义、查找引用、类型检查 |
| **Shell** | `Bash`, `PowerShell`, `Monitor` | 命令执行 + 后台监听 |
| **网络** | `WebSearch`, `WebFetch` | 搜索 + 抓取 |
| **编排** | `Agent` (subagent), `Workflow`, `SendMessage` | 并行/多 agent 协作 |
| **计划** | `EnterPlanMode`, `ExitPlanMode` | 先计划后编码 |
| **交互** | `AskUserQuestion`, `PushNotification`, `SendUserFile` | 用户交互 |
| **调度** | `CronCreate/List/Delete`, `ScheduleWakeup` | 定时任务 |
| **隔离** | `EnterWorktree/ExitWorktree` | Git worktree 隔离 |
| **任务** | `TodoWrite`, `TaskCreate/Get/List/Stop/Update` | 任务清单 |
| **MCP** | `ListMcpResourcesTool`, `ReadMcpResourceTool`, `ToolSearch` | MCP 集成 |
| **其他** | `ReportFindings`, `Artifact`, `ShareOnboardingGuide`, `Skill`, `RemoteTrigger` | 专业功能 |

约 30+ 工具，是功能最全面的参照标杆。

### Cursor Agent

| 工具 | 作用 |
|------|------|
| `list_dir` | 列出目录 |
| `file_search` | 按文件名搜索 |
| `read_file` | 读文件 |
| `grep_search` | 正则搜索 |
| `codebase_search` | 语义搜索 |
| `run_terminal_command` | 执行命令 |
| `edit_file` | 精确编辑 |
| `delete_file` | 删除文件 |

基础 8 工具，依赖 IDE 环境补足 LSP 等能力。

### GitHub Copilot Agent

| 工具 | 作用 |
|------|------|
| `read_file` | 读文件 |
| `edit_file` | 编辑文件 |
| `run_in_terminal` | 终端命令 |

极简三工具，复杂能力由 VS Code 环境提供。

---

## 差距分析与扩展方向

按优先级从高到低排列。

### 🔴 P0 — 核心缺失（建议优先实现）

#### 1. Edit 精确编辑

- **对标**: Claude Code `Edit`, Cursor `edit_file`
- **现状**: 只有 `write_file`（全量覆盖）。修改大文件时浪费大量 token，且容易因全量重写引入无关变更。
- **实现思路**:
  - 基于字符串替换（类似 Claude Code 的 `old_string` → `new_string` 模式）
  - 读后编辑校验（必须读过文件才能编辑）
  - 支持 `replace_all` 批量替换
  - 可复用到 `/edit` 斜杠命令
- **参考**: `tool_registry.py` 中已有的 TEMPLATE_REGEX 逻辑

#### 2. 子 Agent / 并行任务

- **对标**: Claude Code `Agent` (subagent), `Workflow`
- **现状**: agent 单线程执行，无法并行处理独立任务
- **实现思路**:
  - 在当前 ReAct 循环中嵌入子 agent 调度
  - 子 agent 在独立上下文窗口运行，返回最终结果
  - 可设置 `maxTurns` 限制
  - 后台执行 + 通知机制
- **注意**: 需要处理好权限隔离，避免子 agent 越权

#### 3. 任务清单管理

- **对标**: Claude Code `TodoWrite`, `TaskCreate/Get/List/Stop/Update`
- **现状**: agent 靠对话记忆追踪进度，复杂任务容易丢失上下文
- **实现思路**:
  - 新增 `todo_create`、`todo_update`、`todo_list` 工具
  - 数据保存在 `~/.cache/opencode-switcher/tasks.json`
  - 支持依赖关系、状态变更

### 🟡 P1 — 重要增强

#### 4. LSP 代码智能

- **对标**: Claude Code `LSP`
- **现状**: 只有 `grep_search`（文本级），没有代码语义理解
- **实现思路**:
  - 通过 LSP 协议连接语言服务器
  - 支持：跳转到定义、查找引用、获取类型信息、列出文件符号
  - 编辑后自动获取诊断信息（类型错误、警告）
  - 可用 `pylsp`（Python）或 `typescript-language-server`（TS）等
- **依赖**: 需要在系统中安装对应语言的 LSP 服务器

#### 5. 文件管理（删除/重命名/移动）

- **对标**: Cursor `delete_file`
- **现状**: 只能读写，agent 无法管理文件
- **实现思路**:
  - 新增 `delete_file` 和 `rename_file` 工具
  - 遵循现有 `_resolve_safe_path()` 安全路径检查
  - `delete_file` 移动文件到回收站而非直接删除

#### 6. Monitor 后台监听

- **对标**: Claude Code `Monitor`
- **现状**: agent 不能等待事件（日志、文件变化、CI 结果）
- **实现思路**:
  - 后台线程执行命令，输出行逐行回传给 agent
  - 支持 tail 日志文件、轮询 URL、WebSocket
  - 与现有 `bash` 工具的 `run_in_background` 整合
  - WebSocket 源（`ws://` 连接）

### 🟢 P2 — 锦上添花

#### 7. 通知推送

- **对标**: Claude Code `PushNotification`
- **场景**: 后台任务完成、定时任务触发时通知用户
- **实现**: `notify-send`（Linux 桌面通知）

#### 8. 计划模式

- **对标**: Claude Code `EnterPlanMode` / `ExitPlanMode`
- **场景**: 复杂变更先出计划、用户确认后再执行
- **实现**: 新增 `enter_plan_mode` 工具，切换 agent 行为模式

#### 9. Git Worktree / 隔离执行

- **对标**: Claude Code `EnterWorktree` / `ExitWorktree`
- **场景**: 高风险修改在隔离分支进行，不影响主工作区
- **实现**: 利用 `git worktree` 创建临时工作目录

#### 10. 定时调度

- **对标**: Claude Code `CronCreate/List/Delete`
- **场景**: 定时执行任务（每日代码审查、定期清理）
- **实现**: 基于系统 cron 或线程定时器

---

## 技术约束（项目特有）

扩展工具时需注意本项目的特点：

1. **WebView 渲染**: 工具结果以 HTML 片段渲染在 WebView 中。新工具的输出需适配 `render_collapsible_tool_result()` 或 `format_tool_result_for_display()`
2. **ReAct 循环上限**: `ai_tool_loop.py` 中 `run_llm_react_loop` 固定 25 轮迭代上限。子 agent 工具需考虑如何管理自身迭代
3. **SSE 工具调用累积**: `_ToolCallAccumulator` 负责累积 SSE 流式 delta。新工具的 tool call 格式需与现有 JSON schema 一致
4. **无 asyncio**: 项目纯同步，没有事件循环。`Monitor` 等需要异步等待的工具需用线程 + `GLib.idle_add()` 桥接到 GTK 主循环

---

## 参考实现路径

```
Phase 1 (P0)      Edit 精确编辑 + 任务清单管理
Phase 2 (P0)      子 Agent 并行执行
Phase 3 (P1)      LSP 代码智能 + 文件管理
Phase 4 (P1)      Monitor 后台监听
Phase 5 (P2)      通知 / 计划模式 / 隔离执行 / 定时调度
```

每个 Phase 独立可交付，可以按需选择起点。
