# 重构计划：精简 AI 渲染管线，仅保留 full 流式模式

## 背景

当前 `AIChatPanel` 支持三种流式模式（`off` / `text_only` / `full`），由设置 `streaming_v2_mode` 控制。经长期体验验证，`full` 模式（流式文本 + 工具增量更新）体验稳定、性能良好。旧版全量渲染模式 (`off`) 每收到 token 就重建整个对话轮次的 HTML，渲染开销大，应彻底移除。`text_only` 模式是过渡态，也不再需要。

## 目标

- 删除 `off` 和 `text_only` 两种模式的所有代码
- 始终使用 `full` 模式（流式文本 + 推理增量 + 工具调用增量更新）
- 保持 `enable_incremental_tools`（v3 增量卡片）和 `show_tool_details`（是否展开工具详情）作为独立选项
- 精简 `~150` 行冗余代码，消除 8 处 `if _streaming_mode ==` 分支

---

## 变更清单

### 文件 1: `clipboard_store.py` — `AISettingsStore` 数据类

| 改动 | 说明 |
|------|------|
| 删除 `streaming_v2_mode: str = "full"` 字段 | 不再需要选择模式 |
| 删除 `to_dict()` / `from_dict()` 中对应的序列化 | 兼容旧配置读取（静默忽略未知字段即可） |

保持 `enable_incremental_tools` 和 `show_tool_details` 不变。

### 文件 2: `ai_chat_panel.py` — 核心渲染逻辑

#### 2.1 删除类常量（~L95-99）

- 删除 `_STREAM_MODE_OFF = "off"`
- 删除 `_STREAM_MODE_TEXT = "text"`
- 保留 `_STREAM_MODE_FULL = "full"`（或不删除，仅作为文档常量保留但不再使用）

#### 2.2 删除实例变量（~L188）

- `self._streaming_mode = self._STREAM_MODE_OFF` → 删除，不再需要

#### 2.3 简化 `_init_streaming_state()`（~L756-773）

当前：
```python
def _init_streaming_state(self):
    ...
    v2_mode = getattr(self._ai_settings_store, 'streaming_v2_mode', 'full')
    if v2_mode == self._STREAM_MODE_OFF:
        self._streaming_mode = self._STREAM_MODE_OFF
    elif v2_mode == self._STREAM_MODE_TEXT:
        self._streaming_mode = self._STREAM_MODE_TEXT
    else:
        self._streaming_mode = self._STREAM_MODE_FULL
```

改为：删除整个模式选择逻辑，`init_streaming_state` 仅保留 buffer 重置等初始化工作。

#### 2.4 `_on_token_delta()`（~L789）

```python
if self._streaming_mode == self._STREAM_MODE_OFF: return
```
→ 删除这行 guard

#### 2.5 `_flush_token_buffer()`（~L805）

```python
if self._streaming_mode == self._STREAM_MODE_OFF:
    self._token_buffer = ""
    return False
```
→ 删除这段 guard

#### 2.6 `_on_reasoning_delta()`（~L827）

```python
if self._streaming_mode == self._STREAM_MODE_OFF: return
```
→ 删除

#### 2.7 `_flush_reasoning_buffer()`（~L838）

```python
if self._streaming_mode == self._STREAM_MODE_OFF:
    self._reasoning_buffer = ""
    return False
```
→ 删除

#### 2.8 删除 `_switch_to_html_mode()` 方法（~L860-920）

此方法是 TEXT→FULL 的过渡函数。在 TEXT 模式下，工具调用触发时从纯文本流式切换到全量 HTML。Full 模式下**不需要**此方法。

函数头 `_switch_to_html_mode`（~L860）到 `self._streaming_mode = self._STREAM_MODE_FULL`（~L920）整个删除。

#### 2.9 简化 `_on_tool_result()`（~L931-957）

```python
if self._streaming_mode == self._STREAM_MODE_OFF:
    return
```
→ 删除此 guard。保留 `enable_incremental_tools` check 和增量更新逻辑（这是 v3 特性，独立于模式）。

#### 2.10 简化 `_finalize_streaming_render()`（~L1530-1578）

```python
if self._streaming_mode == self._STREAM_MODE_OFF:
    return
```
→ 删除此 guard。方法体保持（buffer flush + final render + cache + JS sync）。

#### 2.11 简化 `_on_llm_api_finished()` 中的旧版路径（~L1770-1795）

```python
if self._streaming_mode == self._STREAM_MODE_OFF:
    msg_id = f"msg-{req_id}"
    ...旧版全量渲染...
```
→ 删除整个 `if _STREAM_MODE_OFF:` 分支。

#### 2.12 简化 `_render_current_assistant_message()`（~L1665-1670）

```python
if self._streaming_mode == self._STREAM_MODE_TEXT:
    ...
```
→ 删除此方法或简化。full 模式下由 token batching 和 `_finalize_streaming_render` 处理，此方法不再需要。

#### 2.13 检查 `_handle_stream_end()`（~L1590）

确认无模式分支（当前只有 `_finalize_streaming_render` 调用）。

### 文件 3: `settings_dialog.py` — 流式输出标签页

#### 3.1 移除流式模式下拉框（~L433-458）

删除：
- `mode_lbl`
- `self._streaming_mode_combo`
- `mode_hint` 中的模式描述

#### 3.2 移除分隔线（~L461-464）

删除 `sep` 分隔线（若下拉框和增量 checkbox 之间不再需要）。

#### 3.3 简化标签页描述文本

将"需在「流式 v2 模式」为 full 时生效"改为"始终生效"。

#### 3.4 `_on_save()` 中删除模式保存（~L826）

```python
streaming_id = self._streaming_mode_combo.get_active_id()
if streaming_id:
    self._ai_settings_store.streaming_v2_mode = streaming_id
```
→ 删除

### 文件 4: `render_pipeline.py` — 渲染管线

无需大改。`_render_standard_mode` 现在是唯一的主路径。`_render_ask_question_mode` 仍需保留（ask_user_question 工具的场景）。

`show_tool_details` 参数保持。

### 文件 5: `ai_chat_panel.py` — 其他引用点

搜索 `_STREAM_MODE_` 和 `_streaming_mode` 的所有使用点，确保无遗漏。

- `_on_llm_error` / `_on_tool_cancel` 等错误路径中若有 `_streaming_mode` 引用，一并清理。

---

## 改动汇总

| 文件 | 删除行 | 修改行 | 新增行 | 净减 |
|------|:------:|:------:|:------:|:---:|
| `clipboard_store.py` | 3 | 0 | 0 | -3 |
| `ai_chat_panel.py` | ~120 | ~30 | 0 | -120 |
| `settings_dialog.py` | ~30 | ~15 | 0 | -30 |
| `render_pipeline.py` | 0 | 0 | 0 | 0 |
| **合计** | ~153 | ~45 | 0 | **~-153** |

---

## 验证方法

1. **编译检查**：`python3 -m py_compile ai_chat_panel.py` 无语法错误
2. **导入检查**：启动应用，确认无 ImportError
3. **功能验证**：
   - 发送一条普通对话消息，确认流式文本正常渲染
   - 触发工具调用（如 web_search），确认工具卡片增量更新正常
   - 触发推理过程，确认 thinking badge 正常显示
   - 切换 `show_tool_details` 设置，保存后重启，确认效果
   - 切换 `enable_incremental_tools` 设置，保存后重启，确认效果
4. **旧配置兼容**：已有 `aisettings.json` 中若包含 `streaming_v2_mode` 字段，不应导致加载失败

---

## 回滚思路

若出现问题，最简单的方式：
1. `git checkout master -- ai_chat_panel.py clipboard_store.py settings_dialog.py`
2. 重启应用
