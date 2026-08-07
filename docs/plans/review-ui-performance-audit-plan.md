# UI 性能瓶颈与代码质量审计修正计划

## 1. 审查摘要

在 `improve-feature-ui-performance-audit` 分支下，由子代理完成了全量代码质量与健壮性审计。
审计识别了 2 项高优先级健壮性隐患、2 项中优先级隐患及 1 项低优先级清理项，需在推送到 `master` 前修复。

---

## 2. 改进问题清单（按优先级分级）

| 编号 | 优先级 | 影响模块 | 问题描述 | 改进方案 |
| :--- | :--- | :--- | :--- | :--- |
| **FIX-01** | 🔴 高 | `stores/clipboard_store.py` & `views/clipboard_panel.py` | 剪贴板分类大小写不一致（`"Text"` vs `"text"`），导致切换 `"text"` / `"image"` 标签页时所有记录被误过滤清空 | 统一使用小写 `"text"` / `"image"`，并在读取 JSON 时针对缺少 `"type"` 键的旧数据补充推断 |
| **FIX-02** | 🔴 高 | `html_templates/chat.js` | `finishReasoning()` 在 `container` 找不到时 early return，导致 `_reasoningCache = ''` 清空被跳过 | 使用 `try { ... } finally { _reasoningCache = ''; _reasoningPendingText = ''; }` 保护清空逻辑 |
| **FIX-03** | 🟡 中 | `views/ai_chat_panel.py` | 切会话时若直接跳过 `_rebuild_markdown_from_messages`，会导致 `_ai_markdown_text` 残留上一会话内容，且 `_update_token_display()` 未刷新 | 保留 `_rebuild_markdown_from_messages` 与 `_update_token_display()` 保证状态一致性 |
| **FIX-04** | 🟢 低 | `views/panel.py` | `views/panel.py` 头部残存无用的 `import gc` | 删除未使用的 `import gc` 引用 |

---

## 3. 分步修改方案

1. **Step 1**：在 `stores/clipboard_store.py` 中将预分类回退值由 `"Text"` 改为小写 `"text"`/`"image"`，并在反序列化前检测 `"type" not in d`；在 `views/clipboard_panel.py` 中回退为小写 `"text"`。
2. **Step 2**：在 `html_templates/chat.js` 的 `finishReasoning()` 中引入 `try...finally`，确保 `_reasoningCache = ''` 无条件执行。
3. **Step 3**：恢复 `views/ai_chat_panel.py` 的 `_ai_markdown_text` 更新与 `_update_token_display()` 调用，并删除 `views/panel.py` 中未使用的 `import gc`。
4. **Step 4**：更新并运行全量单元测试（531 PASS）。

---

## 4. 验证方法

- 运行 `venv/bin/python3 -m unittest discover tests` 确保测试 100% PASS。
- 验证剪贴板面板 Tab 筛选（Text / Image / Code）正常显示。
