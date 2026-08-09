# AI 润色提示词模板提取与自定义功能实施计划

## 1. 需求摘要 (Requirement Summary)
将 `/ai-polish` 命令使用的润色提示词模板独立提取出来，并在 **Settings 模块** 中新增一个 **“润色提示词”** 标签页。
- 默认提供预置润色模板；
- 支持用户在 UI 界面中对该提示词模板进行自由编辑与重置；
- 识别并识别填充两个核心占位符：
  - `{model-last-answer}`：替换为模型最后一次正式回答内容（若无历史回答，自动填入 `(无历史对话，此为首条提问)`）；
  - `{user-original-message}`：替换为用户提交的原始提问文本。

---

## 2. 解决方案设计 (Solution Architecture & Design)

### 2.1 数据存储扩展 (`stores/clipboard_store.py`)
- 定义默认模板常量 `_DEFAULT_POLISH_TEMPLATE`：
  ```markdown
  以下是对话背景：
  ```
  {model-last-answer}
  ```

  ---

  以下是用户的下一轮原始提问：
  ```
  {user-original-message}
  ```

  ---

  请将用户原始提问进行扩展，润色，使其严谨，无歧异。
  仅输出润色后完整改进语句即可。
  ```
- 在 `AISettingsStore` 中新增 `polish_prompt_template: str` 字段，并在 `_load()` 与 `save()` 中实现 JSON 数据持久化（存入 `ai_settings.json`）。

### 2.2 设置界面新增标签页 (`dialogs/settings_dialog.py`)
- 在 `SettingsDialog._tabs` 中追加 `("润色提示词", self._build_polish_prompt_tab)` 标签页。
- 实现 `_build_polish_prompt_tab()`：
  - 提供多行 `Gtk.TextView`（支持代码/单色字体与高亮显示）供用户编辑模板；
  - 增加占位符说明面板（详细标注 `{model-last-answer}` 与 `{user-original-message}` 的语法与含义）；
  - 增加 **“🔄 重置为默认模板”** 按钮，便于用户一键还原；
- 在 `_on_save()` 保存逻辑中，读取 `self._polish_prompt_view` 缓冲文本并存入 `self._ai_settings_store.polish_prompt_template`。

### 2.3 斜杠命令与占位符动态填充 (`views/ai_chat_panel.py`)
- 在 `_handle_ai_polish_command(raw_input)` 中：
  1. 读取 `self._ai_settings_store.polish_prompt_template`；
  2. 获取上一轮模型正式回答 `last_asst_text`。若为空（首条提问），设为 `last_answer_fill = "(无历史对话，此为首条提问)"`；
  3. 执行占位符精确替换：
     ```python
     prompt = (
         template
         .replace("{model-last-answer}", last_answer_fill)
         .replace("{user-original-message}", raw_input)
     )
     ```
  4. 将替换完成后的标准 Prompt 发送给润色模型。

---

## 3. 全量代码改动梳理 (Code Change Mapping)

| 序号 | 目标文件路径 | 涉及类 / 方法 | 具体改动说明 | 潜在影响/依赖 |
| :--- | :--- | :--- | :--- | :--- |
| **1** | [`stores/clipboard_store.py`](file:///home/hzb/opencode-switcher/stores/clipboard_store.py#L1500) | `_DEFAULT_POLISH_TEMPLATE`<br>`AISettingsStore` | 1. 定义 `_DEFAULT_POLISH_TEMPLATE` 常量；<br>2. `AISettingsStore` 增加 `polish_prompt_template` 属性，并在 `_load()` / `save()` 中完成读写。 | 自动兼容 `ai_settings.json` 无破坏变更。 |
| **2** | [`dialogs/settings_dialog.py`](file:///home/hzb/opencode-switcher/dialogs/settings_dialog.py#L75) | `SettingsDialog` | 1. 在 `self._tabs` 中注册 `("润色提示词", self._build_polish_prompt_tab)`；<br>2. 实现 `_build_polish_prompt_tab()`（TextView + 占位符提示 + 重置按钮）；<br>3. 在 `_on_save()` 中保存文本内容。 | 扩展设置面板，界面干净美观。 |
| **3** | [`views/ai_chat_panel.py`](file:///home/hzb/opencode-switcher/views/ai_chat_panel.py#L3730) | `AIChatPanel._handle_ai_polish_command` | 1. 读取自定义模板；<br>2. 替换 `{model-last-answer}` 与 `{user-original-message}` 占位符。 | 增强 Prompt 组装弹性。 |
| **4** | `tests/test_ai_polish_command.py` | `TestAIPolishCommand` | 1. 增加对自定义模板及占位符替换逻辑的单元测试。 | 全量测试 Pass。 |

---

## 4. 分阶段实施计划

1. **阶段 1：数据存储层**（`stores/clipboard_store.py`）：添加默认模板与 `AISettingsStore` 持久化字段。
2. **阶段 2：设置 UI 标签页**（`dialogs/settings_dialog.py`）：新建“润色提示词”标签页、TextView、重置按钮与保存逻辑。
3. **阶段 3：命令处理逻辑**（`views/ai_chat_panel.py`）：切换为模板驱动 + 占位符替换机制。
4. **阶段 4：单元测试与回归**（`tests/test_ai_polish_command.py`）：验证占位符替换、缺省回退与全量测试套件通过。
