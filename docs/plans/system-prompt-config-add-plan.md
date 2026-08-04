# 系统提示词配置标签页 — 实施计划 (system-prompt-config-add-plan)

## 需求摘要

在 Settings 对话框新增「系统提示词」标签页，允许用户编写全局系统提示词（system prompt），
配置后仅对新建立的 AI 对话生效；已存在的对话使用自身快照，不热加载新配置，
以保证 LLM 请求前缀稳定、prompt 缓存可命中、不浪费 token。

## 设计决策（已确认）

- **方案 A**：会话级快照 + 请求层注入（extra_system_messages），不写入 `_ai_messages`。
- 注入时机语义：system prompt 在**新对话建立时从 Settings 快照固化**，该对话此后每轮请求
  沿用同一快照（前缀稳定 → 缓存命中）；旧对话加载自身持久化快照（`Conversation.system_prompt`），
  绝不读取 Settings 当前值 → 无热加载。
- 空 system prompt（含旧对话无快照）→ 不注入，行为与现状完全一致（向后兼容）。

## 分阶段执行步骤

### 阶段 1：数据层 — AISettingsStore 新增字段（0.5h）
文件：`stores/clipboard_store.py`（`AISettingsStore`，L1189 起）
- `__init__` 加 `self.system_prompt: str = ""`
- `_load()` 加 `self.system_prompt = data.get("system_prompt", "")`
- `save()` 加 `"system_prompt": self.system_prompt`，`version` 4 → 5

### 阶段 2：UI 层 — Settings 新标签页（1h）
文件：`dialogs/settings_dialog.py`
- `self._tabs` 注册表追加 `("系统提示词", self._build_system_prompt_tab)`
- 新方法 `_build_system_prompt_tab()`：多行 `Gtk.TextView`（monospace、wrap）+ 说明文案
  （注入时机语义：仅新对话生效、旧对话不受影响、保存后新对话立即生效）+ 预填当前值
- `_on_save()`：`self._ai_settings_store.system_prompt = textview 内容`
- 可选：插入 `{clipboard}` 占位符提示（如需要，与模板语法对齐后再加）

### 阶段 3：注入层 — 会话快照与请求注入（1.5h）
文件：`views/ai_chat_panel.py`
- `__init__`（L129 附近）：`self._ai_system_prompt: str = ""`
- `_start_new_conversation()`（L731）：`self._ai_system_prompt = AISettingsStore().system_prompt`
  （用 `self._ai_settings_store` 或懒加载，注意避免循环依赖）
- `_reset_ai_panel_silent()`（L3981，/new、/delete 后新会话）：同样刷新快照
- `_switch_to_conversation()`（L3631，两个分支：streaming 恢复 + 常规加载）：
  `self._ai_system_prompt = conv.system_prompt or ""`（旧对话快照，不读 Settings）
- `_build_llm_messages()`（L763）：extra 列表**首项**注入
  `{"role": "system", "content": self._ai_system_prompt}`（非空时），历史摘要排在其后
- `_save_current_conversation()`（L3589，两条路径）：
  - `create_conversation(..., system_prompt=self._ai_system_prompt)`
  - else 分支：`conv.system_prompt = self._ai_system_prompt`（含新建 Conversation 兜底分支）

### 阶段 4：验证（0.5h）
- 单测：`venv/bin/python3 -m unittest discover tests`（现有 14 个文件，确认无回归）
- 手动验证清单（见测试策略）
- 提交：`feat(ai-panel): add system prompt config tab with per-conversation snapshot`

## 技术要点与风险提示

- **注入顺序**：`llm_client._build_request`（L375-378）把 extra 注入在传入 messages 之前，
  system prompt 在 extra[0] → 请求体最前 ✓；与 skills 注入（`ai_tool_loop` 首轮 append 到
  messages 末尾）互不冲突。
- **循环依赖**：ai_chat_panel 顶部已有 `from stores.clipboard_store import ...` 先例，
  `AISettingsStore()` 在方法内懒加载，避免 import 周期。
- **风险点 1（热加载）**：`_reset_ai_panel_silent` 也被 `/fork` 前序流程间接触发？——
  已确认 fork 走 `_switch_to_conversation(new_conv.id)` 加载新分支（store 层 fork 复制
  `system_prompt`），快照继承正确；但需在阶段 3 回归测试 fork 后 system prompt 是否等于原对话。
- **风险点 2（重试/回滚）**：`_retry_response`、`/rollback` 走 `_build_llm_messages`，
  快照不变 → 注入稳定，无需额外处理。
- **风险点 3（旧对话空快照）**：历史已存在对话 `system_prompt=""` → 不注入，行为不变。
- 不动 `_ai_messages`，标题提取（`_extract_local_title`）、DOM 渲染、`/summary` 零影响。

## 依赖项

- 无外部依赖；`ai_settings.json` 自动兼容旧文件（`data.get` 默认值）。
- 无需数据库迁移（SQLite 不涉及，仅 JSON 配置）。

## 验收标准

1. Settings 出现「系统提示词」标签页，可编辑多行文本，保存后重启仍保留（持久化）。
2. 保存非空 system prompt 后，**新建对话**首轮及后续轮次的 API 请求 messages[0] 为该 system 消息，
   且每轮前缀一致。
3. 已存在的旧对话切换回来看不到新 system prompt（快照隔离，无热加载）。
4. 保存为空字符串 → 不注入任何 system 消息（向后兼容）。
5. `/fork` 分支继承原对话 system prompt；`/retry`、`/rollback` 不改变注入。
6. 与历史摘要（/summary）共存时：请求体顺序为 [system_prompt, 历史摘要, 对话消息]。
7. 现有测试全部通过，无回归。

## 测试策略

- **单元测试**：新增 `tests/test_system_prompt.py`（gitignored，参照现有测试风格）：
  - AISettingsStore 读写 round-trip；空值兼容
  - `_build_llm_messages` 注入逻辑（mock AISettingsStore / 直接构造实例）
- **手动测试**：
  1. 设置 → 系统提示词 → 写入"你是一个资深 Python 工程师" → Save
  2. 新对话发消息 → 观察 `run.log` 或抓包确认首条为 system
  3. 修改 system prompt → 切回旧对话发消息 → 确认旧对话仍用旧快照
  4. /fork、/retry、/rollback、/new 各触发一次
  5. 清空 system prompt → 新对话无注入

## 回滚或调整思路

- **代码回滚**：`git revert` 该提交即可（改动集中在 3 个文件，无迁移副作用）。
- **逻辑调整**：若产品上希望"修改 Settings 后所有对话都生效"（放弃缓存优化），
  只需把 `_switch_to_conversation` 的快照读取改为 `AISettingsStore().system_prompt`，
  其余代码不动——快照机制保留为开关。
- **数据清理**：无需；`system_prompt` 字段空值即未启用。
