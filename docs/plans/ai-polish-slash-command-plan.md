# `/ai-polish` (AI 提问润色斜杠命令) 全量代码改动与实施计划

## 1. 全面梳理改动点 (Full Code Mapping)

| 序号 | 目标文件路径 | 涉及类 / 函数 / 方法 | 具体修改内容 (行号与逻辑) | 模块依赖关系与潜在影响 |
| :--- | :--- | :--- | :--- | :--- |
| **1** | [`stores/clipboard_store.py`](file:///home/hzb/opencode-switcher/stores/clipboard_store.py) | `LLMModelConfig`<br>`LLMSettingsStore` | 1. **L1186**: 为 `LLMModelConfig` 新增 `is_polish_default: bool = False` 属性。<br>2. **L1228**: 在 `_load()` 方法中解析 JSON 的 `is_polish_default` 字段。<br>3. **L1285+**: 为 `LLMSettingsStore` 新增 `get_polish_model()` 与 `set_polish_default(alias)` 方法。 | 无负面影响。`asdict` 自动持久化到 `llm_settings.json`。 |
| **2** | [`dialogs/prompts_config_dialog.py`](file:///home/hzb/opencode-switcher/dialogs/prompts_config_dialog.py) | `show_prompts_config_dialog` | 1. **L489, L547**: 新增 `polish_check` 复选框 (显示名称：“润色默认模型”)，绑定数据存储。<br>2. **L503**: 在模型列表 `_model_label()` 中支持 `(润色)` 列标记说明。<br>3. **L710+**: 新增 `on_polish_toggled(widget)`，实现与其他模型复选框一致的单选互斥逻辑。 | 纯 UI 设置增强，无破坏风险。 |
| **3** | [`views/ai_chat_panel.py`](file:///home/hzb/opencode-switcher/views/ai_chat_panel.py) | `AIChatPanel` | 1. **L125+**: 在 `_AI_COMMANDS` 列表中注册 `("/ai-polish", ...)`。<br>2. **L2620+**: 在 `_on_send_clicked` 中捕获 `/ai-polish <raw_text>` 命令，阻断主对话提交，转交 `_handle_ai_polish_command(raw_text)`。<br>3. **新增 `_handle_ai_polish_command`**: 从 `_ai_messages` 获取最近一次模型正式回答；进行动态降级 Prompt 构建；切换输入框为青绿色状态并禁用；开启 30s 后台看门狗请求 `get_polish_model()`，成功时将优化文本自动回填输入框，失败/超时复原原始文本。 | 依赖 `LLMSettingsStore` 与 `_llm_client`；主线程安全需依靠 `GLib.idle_add`。 |
| **4** | [`views/clipboard_panel.py`](file:///home/hzb/opencode-switcher/views/clipboard_panel.py) | `ClipboardPanel` | 1. **L115+**: 同步 `_AI_COMMANDS` 列表注册 `/ai-polish` 命令提示。 | 保证各面板补全提示一致。 |
| **5** | `tests/test_ai_polish_command.py` | `TestAIPolishCommand` | 1. 新建单元测试文件，涵盖命令解析、Prompt 组装、首条提问降级、润色模型回退与超时回退等场景。 | 无 |

---

## 2. 详细实施计划 (Detailed Step-by-Step Implementation Plan)

### 步骤一：数据存储层扩展 (`stores/clipboard_store.py`)
- **操作目标**：使 LLM 配置模型具备 `is_polish_default` 标识，并支持高效获取润色模型。
- **涉及位置**：`stores/clipboard_store.py` (L1186, L1228, L1285)
- **改动说明**：
  ```python
  @dataclass
  class LLMModelConfig:
      ...
      is_polish_default: bool = False  # 标记为润色默认模型

  class LLMSettingsStore:
      def get_polish_model(self) -> Optional[LLMModelConfig]:
          model = next((m for m in self.models if getattr(m, "is_polish_default", False)), None)
          if not model:
              model = next((m for m in self.models if m.is_default), None)
          if not model and self.models:
              model = self.models[0]
          return model
  ```

### 步骤二：UI 设置对话框配置扩展 (`dialogs/prompts_config_dialog.py`)
- **操作目标**：在 API Settings 面板中加入“润色默认模型”复选框及其单选逻辑。
- **涉及位置**：`dialogs/prompts_config_dialog.py` (L489, L503, L547, L715)
- **改动说明**：
  在模型编辑区域添加 `polish_check = Gtk.CheckButton.new_with_label("润色默认模型")`，并在 `on_polish_toggled` 中设置选中该模型时清空其他模型的 `is_polish_default` 标记。

### 步骤三：斜杠命令与异步润色主管道 (`views/ai_chat_panel.py`)
- **操作目标**：实现 `/ai-polish` 命令识别、模板构建、青绿色状态切换、30s 超时控制与文本自动回填。
- **涉及位置**：`views/ai_chat_panel.py` (L125, L2620, 新增 `_handle_ai_polish_command`)
- **伪代码/关键逻辑**：
  ```python
  def _handle_ai_polish_command(self, raw_input: str):
      # 1. 查找最后一次 assistant 消息正文
      last_asst_text = ""
      for msg in reversed(self._ai_messages):
          if msg.get("role") == "assistant" and msg.get("content"):
              last_asst_text = msg.get("content")
              break

      # 2. 动态模板构建（处理首条消息边界）
      if last_asst_text:
          prompt = f"以下是对话背景：\n```\n{last_asst_text}\n```\n\n---\n\n以下是用户的下一轮原始提问：\n```\n{raw_input}\n```\n\n---\n\n请将用户原始提问进行扩展，润色，使其严谨，无歧异。\n仅输出润色后完整改进语句即可。"
      else:
          prompt = f"以下是用户的原始提问：\n```\n{raw_input}\n```\n\n---\n\n请将用户原始提问进行扩展，润色，使其严谨，无歧异。\n仅输出润色后完整改进语句即可。"

      # 3. 设置青绿色等待状态并禁用输入框
      buf = self._ai_entry.get_buffer()
      buf.set_text("")
      self._ai_entry.placeholder_text = "✨ 等待 AI 润色中..."
      self._ai_entry.set_sensitive(False)
      self._update_send_button(False, sensitive=False)

      # 4. 开启 30s 线程与超时处理
      polish_model = self._llm_settings_store.get_polish_model()
      
      def run_polish_thread():
          # 异步请求 completions API, 30s 超时
          # 成功 -> GLib.idle_add(on_success, content)
          # 失败/超时 -> GLib.idle_add(on_failure, raw_input)
  ```

### 步骤四：剪贴板面板命令同步 (`views/clipboard_panel.py`)
- **操作目标**：同步 `_AI_COMMANDS` 定义。
- **涉及位置**：`views/clipboard_panel.py` (L115)

### 步骤五：单元测试与全量回归 (`tests/test_ai_polish_command.py`)
- **操作目标**：新建自动化测试，测试命令解析、降级 Prompt 生成、模型选择与超时异常处理。
- **验证命令**：`venv/bin/python3 -m unittest discover tests`

---

## 3. 预期风险与回退策略 (Risks & Rollback)
- **风险**：网络连接不稳定导致 30 秒超时。
- **回退策略**：超时捕获触发后，自动把 `raw_input` 文本还原回 `_ai_entry`，并弹 Toast 提示“AI 润色超时，已恢复原始提问”，不阻塞正常消息发送。
