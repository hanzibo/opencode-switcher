# 🤖 Subagent 实时监察机制增强与迭代限制放开实施计划

本文档为 OpenCode Switcher 的子代理（Subagent）实时监察机制增强与最大迭代次数限制放开的实施方案。

---

## 📋 需求摘要

1. **子代理三态实时监察（Thinking / Tool Call / Answering）**：
   - 将子代理气泡在 `running` 状态下的不透明模糊描述，升级为基于 ReAct 循环事件驱动的**实时动作监控**（三类动作：`Thinking` 思考中、`Tool Call: <tool_name>` 工具调用中、`Answering` 生成回答中）。
   - Tooltip 清理：移除现有的任务截断描述，保留子代理 ID，突出显示当前正在进行的实时动作。
2. **共用主代理最大迭代限制**：
   - 移除 `tool_registry/subagent.py` 中硬编码的 `_MAX_SUBAGENT_TURNS = 20` 上限限制。
   - 统一读取 `AISettingsStore().max_tool_iterations`，使子代理与主代理共享统一的最大迭代次数配置。

---

## 📐 架构与数据流设计

```mermaid
graph TD
    subgraph 后台执行引擎 (tool_registry/subagent.py)
        A[execute_sub_agent] --> B[_run_subagent_background]
        B --> C[ReAct 流式迭代循环]
        C -->|推理 Chunk| D1[更新 action: 'Thinking']
        C -->|工具调用事件| D2[更新 action: 'Tool Call: <tool_name>']
        C -->|回答 Chunk| D3[更新 action: 'Answering']
        D1 & D2 & D3 --> E[_notify_subagent_status_change]
    end

    subgraph 前端 GUI 状态栏 (views/ai_chat_panel.py)
        E -->|GLib.idle_add| F[_on_subagent_status_changed]
        F --> G[更新 _ai_subagent_bar 气泡 Tooltip & 标签]
        G --> H[展示子代理实时动作如: sa_1 🔧 Tool Call: bash]
    end
```

---

## 🚀 阶段实施步骤

### 📍 阶段 1：子代理迭代限制放开 (`tool_registry/subagent.py`)
- 移除硬编码 `_MAX_SUBAGENT_TURNS = 20`。
- 修改 `_execute_subagent_sync` 与 `execute_sub_agent`，动态读取 `AISettingsStore().max_tool_iterations` 作为最大迭代上限。

### 📍 阶段 2：实时三态事件抓取与广播 (`tool_registry/subagent.py`)
- 在 `_execute_subagent_sync` 的流式/单轮迭代中引入事件状态追踪：
  - 进入 LLM 思考阶段/收到思考 Chunk 时代入 `action="Thinking"`。
  - 进入工具执行阶段时带入 `action=f"Tool Call: {tc_name}"`（含具名工具名称，如 `Tool Call: bash`）。
  - 进入回答生成阶段/收到回答 Chunk 时代入 `action="Answering"`。
- 在状态字典 `_background_subagent_status[subagent_id]` 中新增 `"action"` 字段，并通过 `_notify_subagent_status_change` 实时广播。

### 📍 阶段 3：GUI 状态栏 Tooltip 与标签实时更新 (`views/ai_chat_panel.py`)
- 修改 `_create_subagent_block` 与 `_update_subagent_block`：
  - 更新 Tooltip 内容为：`子代理 {sid}\n动作：{action}`（格式干净，彻底移除原任务截断描述）。
  - 在 `running` 气泡标签上展示更细粒度的图标与实时动作，例如：`sa_1 🧠 Thinking`, `sa_1 🔧 Tool Call: bash`, `sa_1 💬 Answering`。

### 📍 阶段 4：单元测试编写与验证
- 编写 `tests/test_subagent_monitoring.py` 验证：
  - 迭代上限正确同步 `AISettingsStore` 配置。
  - 状态广播事件能准确携带 `Thinking`, `Tool Call: <name>`, `Answering` 动作信息。
- 全量运行 79+ 单元测试。

---

## 🧪 测试策略与回退思路

1. **单元测试**：运行 `venv/bin/python3 -m unittest discover tests` 确保测试 100% PASS。
2. **手动交互测试**：
   - 触发耗时子代理任务（如 `sub_agent(type="explore", task="搜索所有 python 文件")`）。
   - 观察输入框顶部的 `_ai_subagent_bar` 气泡及 Hover Tooltip 是否能实时从 `Thinking` -> `Tool Call: glob_find` -> `Answering` 动态更新。
3. **回退思路**：修改点集中于子代理状态广播与 Tooltip 模板，风险极低，可通过 Git 快速撤销。
