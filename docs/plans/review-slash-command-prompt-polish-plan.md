# 代码质量审查与改进计划书 (`add-feature-slash-command-prompt-polish`)

## 1. 审查范围清单 (Review Scope)

| 序号 | 文件路径 | 模块 / 关联点 | 审查状态 |
| :--- | :--- | :--- | :--- |
| 1 | [`stores/clipboard_store.py`](file:///home/hzb/opencode-switcher/stores/clipboard_store.py) | `LLMModelConfig` / `AISettingsStore` 数据模型与持久化 | ✅ 已审查 |
| 2 | [`dialogs/prompts_config_dialog.py`](file:///home/hzb/opencode-switcher/dialogs/prompts_config_dialog.py) | API Settings 润色模型复选框与删除逻辑 | ✅ 已审查 |
| 3 | [`dialogs/settings_dialog.py`](file:///home/hzb/opencode-switcher/dialogs/settings_dialog.py) | Settings 窗口“润色提示词”标签页与保存 | ✅ 已审查 |
| 4 | [`views/ai_chat_panel.py`](file:///home/hzb/opencode-switcher/views/ai_chat_panel.py) | 斜杠命令拦截、异步看门狗与占位符填充 | ✅ 已审查 |
| 5 | [`views/clipboard_panel.py`](file:///home/hzb/opencode-switcher/views/clipboard_panel.py) | `_AI_COMMANDS` 描述文本同步 | ✅ 已审查 |
| 6 | [`tests/test_ai_polish_command.py`](file:///home/hzb/opencode-switcher/tests/test_ai_polish_command.py) | 自动化单元测试覆盖率 | ✅ 已审查 |

---

## 2. 6 维度质量评估总结 (6-Dimensional Summary)

1. **可读性 (Readability)**：代码注释清晰，方法职责单一，符合 Pythonic 规范。
2. **可维护性 (Maintainability)**：数据层、UI 设置层与业务逻辑层解耦良好，支持用户自定义模板与占位符重置。
3. **健壮性 (Robustness)**：处理了首条提问边界条件与 30s 超时处理；需要注意多对话切换时的输入框跨会话污染。
4. **性能 (Performance)**：润色耗时操作完全运行在后台独立线程，不会阻塞 GTK 主界面。
5. **安全性 (Security)**：错误消息在注入 WebKit HTML 时存在未彻底 `html.escape` 的小隐患。
6. **一致性 (Consistency)**：严格遵循 PyGObject 主线程调度规范 (`GLib.idle_add`)。

---

## 3. 优先级排序的问题清单 (Prioritized Findings)

### 🔴 高优先级 (High Priority)
1. **[安全性] `_handle_ai_polish_command` 错误消息未彻底转义注入 HTML (XSS 隐患)**
   - **位置**：`views/ai_chat_panel.py` (`_on_polish_complete` 内)
   - **描述**：若底层 HTTP 请求报错包含裸 HTML 或特殊字符（例如 `HTTP 500: <html>...</html>`），`f'⚠️ <strong>AI 润色失败</strong>（{err_msg}）'` 会未经过 `html.escape` 直接注入 WebKit 渲染，可能引发 DOM 结构破坏或 XSS。
   - **方案**：使用 `html.escape(err_msg)` 转义后注入。

2. **[健壮性] 异步润色期间切换对话导致输入框回填错乱 (会话竞态风险)**
   - **位置**：`views/ai_chat_panel.py` (`_handle_ai_polish_command`)
   - **描述**：在 30 秒润色等待期间，若用户通过侧边栏切换了当前 active 会话，子线程回调 `_on_polish_complete` 依然会强行将上一个会话的润色文本填入当前会话的 `_ai_entry` 输入框。
   - **方案**：在启动润色前记录 `target_conv_id = self._ai_conversation_id`，在 `_on_polish_complete` 中仅当 `self._ai_conversation_id == target_conv_id` 时才填入输入框，否则忽略。

---

### 🟡 中优先级 (Medium Priority)
3. **[可维护性/易用性] 占位符名称下划线与连字符容错**
   - **位置**：`views/ai_chat_panel.py`
   - **描述**：用户在自定义模板时，可能会习惯性输入 `{model_last_answer}` 或 `{user_original_message}`（使用下划线而非中划线）。
   - **方案**：统一将中划线与下划线均识别并替换，例如同时替换 `{model-last-answer}` 和 `{model_last_answer}`。

---

### 🟢 低优先级 (Low Priority)
4. **[健壮性] 删除模型时未重新指定 `is_polish_default`**
   - **位置**：`dialogs/prompts_config_dialog.py` (`on_delete_model_clicked`)
   - **描述**：当被删除的模型恰好是润色默认模型时，删除后未像 `is_default` 一样自动将润色默认标记赋予剩余首个模型。
   - **方案**：在删除已被标记为 `is_polish_default` 的模型时，自动将 `local_models[new_idx].is_polish_default = True`。

5. **[一致性] `_AI_COMMANDS` 命令描述文案微小差异**
   - **位置**：`views/clipboard_panel.py` vs `views/ai_chat_panel.py`
   - **描述**：`clipboard_panel.py` 中 `/ai-polish` 文案为 `"扩展润色提问"`，而 `ai_chat_panel.py` 中为 `"扩展润色提问，去除歧义与不严谨"`。
   - **方案**：将两处文案统一。

---

## 4. 分步骤改进实施计划 (Step-by-Step Fix Plan)

1. **步骤 1**：在 `views/ai_chat_panel.py` 中增加 `html.escape(err_msg)` 转义，并引入 `target_conv_id` 校验防跨会话污染。
2. **步骤 2**：在 `views/ai_chat_panel.py` 中增强占位符容错（支持 `_` 与 `-` 两种格式）。
3. **步骤 3**：在 `dialogs/prompts_config_dialog.py` 中补全删除模型时的 `is_polish_default` 自动回退。
4. **步骤 4**：在 `views/clipboard_panel.py` 中同步描述文案。
5. **步骤 5**：运行全量自动化测试，确保 100% 通过。
