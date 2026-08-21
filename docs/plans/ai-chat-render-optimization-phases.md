# AI 助手看盘 — 分步优化方案

> 基于 `ai-chat-render-perf-analysis.md` 的 P0-P2 瓶颈，按 ROI/风险分 4 阶段落地。分支 `analyze/ai-chat-render-perf`，每阶段可独立合入、独立回滚。

## 总则

* **度量先行**：Phase 0 埋点 2 天，不改行为，只加 `time.perf_counter` + `performance.mark`，产出基线（切换/流式/收尾 三档对话各 10 次 P50/P95）。
* **主线程零增长**：任何 Markdown/KaTeX 不得在 `GLib.idle_add` 同步执行超过 50ms；超限改后台线程或增量。
* **约束**：`get_shared_web_context()` 单例、`MemoryPressureSettings` 构造时固定、`GLib.idle_add` 内不改布局、PyGObject 异常看 `run.log`。

---

## Phase 1 — 去重与增量补齐（1-2 天，P0，零风险高收益）

> 目标：砍掉 50% 的全量 Markdown，补齐 assistant tool_calls 增量。

### 1A 去重收尾双全量

* **文件**：`views/ai_chat/runner_mixin.py:332-400`
* **改动**：
  1. `_finalize_streaming_render` 内 `render_turn` 已产 `output.combined_html`，让 `_append_assistant_turn_to_cache(reused_html=output.combined_html, reused_markdown=rebuilt)` 直接写入 `_ai_html_cache`，不再二次 `_markdown_to_html_safe`。
  2. 或抽 `_build_final_html(turn_msgs, streaming_content)` 供两处复用。
* **验收**：`_markdown_to_html_safe` 调用计数收尾阶段 2→1；长对话收尾主线程 -80~200ms。

### 1B assistant tool_calls 增量

* **文件**：`views/ai_chat/runner_mixin.py:81-102`, `views/ai_chat/webview_mixin.py:172`, `html_templates/chat.js:390-423`
* **改动**：
  * Python 侧 `append_message_callback` 判 `msg["role"]=="assistant" and msg.get("tool_calls")` 且 `enable_inc && dom_ready` 时走新路径 `GLib.idle_add(_append_tool_calls_incremental, tool_calls)`，否则回退全量。
  * JS 新增 `appendToolCalls(msgId, cardsHtml)`：对 `msg-bubble .tool-region .tool-steps-container` 做 `insertAdjacentHTML('beforeend', cardsHtml)`，仅对新增卡片调 `addCopyButtons(newNodes)`/`_wrapTables(newNodes)`，不扫全 bubble。
  * `_render_tool_step` 已支持单卡，不新增 Python 渲染。
* **验收**：25 轮 ReAct 全量次数 25→0（仅 tool result 单卡）；`_render_current_assistant_message` 调用计数 -95%。

### 1C 防回退守卫

* **文件**：`streaming_mixin.py:176 _on_tool_result` 已有快速路径，补 `dom_ready` 判空回退日志 `print("[perf] fallback full render...")` 便于回归检测。

**Phase 1 合入条件**：基线对比收尾耗时 -40% 以上，且 `enable_incremental_tools=false` 时行为不变（全量回退）。

---

## Phase 2 — JS 局部化与 KaTeX 精确化（2-3 天，P1，中收益）

> 目标：把 `updateMessageContainer` 从 O(全 bubble) 降到 O(新增节点)。

### 2A 局部 copy/wrap

* **文件**：`html_templates/chat.js:424-468, 470-529, 889-905`
* **改动**：
  * `updateMessageContainer` 三区替换后，`addCopyButtons`/`_wrapTables` 只传 `regions[1]` (tool) 或 `answer` 新节点，而非 `div` 全量；`updateToolCard` 已局部化，保持。
  * `addCopyButtons(root)` 加 `root.querySelectorAll('pre:not(.has-copy-btn):not(.tool-result-content)')` 保持幂等，但上层传入 `newDetails` 时仅扫子树。
* **风险**：`has-copy-btn` 标记依赖，需验证重试/回滚后不漏按钮。

### 2B KaTeX 精确化

* **文件**：`html_templates/chat.js:46-56, 209-225, 432, 467`
* **改动**：
  * 流式期不扫：`_debouncedRenderMath` 已 800ms debounce，补充 `if (!element.textContent.includes('$') && !element.textContent.includes('\\')) return;` 快速跳过无公式 bubble。
  * `updateContent` 同步 `_renderMath` 改为 `requestAnimationFrame` 异步，避免首帧阻塞；或对 >30KB HTML 分两帧。
* **验收**：无公式对话 JS 耗时 -30~50%，有公式对话流式不抖。

### 2C 滚动/分层节流收紧

* **文件**：`chat.js:58 _throttledWindowing`, 243 `_scrollToBottom`, 602 `_updateRoundNav`
* **改动**：已 RAF 节流，补充 `updateMessageContainer` 内 `if (_isStreaming && _autoScroll) skip _updateRoundNav`（已有 617 fast-path，扩大到 tool 卡阶段）。

**Phase 2 合入条件**：`performance.measure(updateMessageContainer)` P95 -30%，功能回归（复制/表格滚动/公式）通过。

---

## Phase 3 — 主线程外 Markdown（3-5 天，P1，高收益高风险）

> 目标：Markdown 不再阻塞 GTK 主循环。

### 3A 后台线程 Markdown + 主线程仅桥接

* **文件**：`views/ai_chat/webview_mixin.py:120 _render_markdown`, `172 _render_current_assistant_message`, `ai_text_utils/markdown.py:74`
* **方案**：
  * 抽 `def render_markdown_async(text, callback)`：后台 `threading.Thread` → `_markdown_to_html_safe` → `GLib.idle_add(callback, html)`。
  * `_render_current_assistant_message` 与 `_render_markdown` 改异步：先 `run_javascript` 占位，回调再 `updateMessageContainer`。
  * `markdown` 库非线程安全，简单加 `threading.Lock` 串行化；或每线程 `import markdown` 独立实例（`_get_markdown_lib` 已懒加载，需测并发）。
* **降级**：若锁竞争高，回退到 `GLib.idle_add` 分片（每 100 行 `yield`），但优先线程池。
* **风险**：时序（快速切换对话导致回调落到旧 `msgId`）需 `req_id` 守卫已在 `webview_mixin.py:179`，扩展到回调。

### 3B Pygments 可选

* **文件**：`ai_text_utils/markdown.py:32`
* **改动**：`_get_markdown_extensions` 对超长内容（>8000 字符或代码块>5）加开关 `enable_codehilite=False`（读 `AISettingsStore().enable_highlight`），或后台线程内 `HtmlFormatter` 缓存已做。

**Phase 3 合入条件**：主线程单帧 >50ms 次数清零；`run.log` 无 `markdown` 竞态异常；`tests/test_ai_text_utils.py` 并发测试通过。

---

## Phase 4 — 真虚拟化与过桥瘦身（5-7 天，P2，长期）

> 目标：50 轮对话 DOM/内存 O(可见轮) 而非 O(全量)。

### 4A 真虚拟化

* **文件**：`html_templates/chat.js:260 applyWindowing`, `showOlderBatch`, `showAllMessages`, `webview_mixin.py:120`
* **方案**：
  * `applyWindowing` 改为 `remove()` 非可见 `msg-row` 并缓存 `outerHTML` 到 `Map<msgId, html>`，`showOlderBatch` 再 `insertAdjacentHTML` 回填；或直接让 Python 侧 `updateContent` 只下发最近 10 轮（`_rebuild_markdown_from_messages` 加 `max_rounds` 参数），更早轮按需 `GLib.idle_add` 分页拉取。
  * 保留 `display:none` 作为 Phase 4A 前的兼容开关 `USE_TRUE_VIRTUALIZATION`。
* **验收**：50 轮 DOM 节点 50→10，`querySelectorAll` 耗时 -70%，WebKit RSS -20~30%。

### 4B 过桥瘦身

* **方案**：`updateContent` 对 >50KB HTML 分片 `requestAnimationFrame` 注入，或 `render_turn` 产 `diff`（仅 answer 区）而非全量；短期可 `json.dumps(..., ensure_ascii=False)` 减转义。

### 4C 持久化分层

* **方案**：`stores/clipboard_store.py` 对超长会话按轮次文件分片，`_load_conversation` 懒加载，避免 `_rebuild_markdown_from_messages` 每次全拼接。

**Phase 4 合入条件**：可选，待 Phase 1-3 基线达标后再评估；需配套 `tests/test_conversation_index.py` 扩展。

---

## 4. 执行顺序与依赖

```
Phase 0 埋点（并行，不阻塞）
  ↓
Phase 1A+1B 可并行（无依赖，必做）
  ↓
Phase 2A/2B（依赖 Phase 1 完成，否则全量基数过大掩盖收益）
  ↓
Phase 3A（依赖 Phase 1，独立于 Phase 2，可与 Phase 2 并行开发但串行合入）
  ↓
Phase 4（依赖 Phase 1-3 全部合入，单独分支验证）
```

* 单阶段提交不超过 300 行，便于 review。
* 每阶段更新 `docs/plans/ai-chat-render-perf-analysis.md` 测量表格。

## 5. 验证矩阵（每阶段必跑）

| 场景 | 指标 | 工具 |
|------|------|------|
| 短对话 1轮 | 切换耗时 `updateContent` | `performance.measure` + `time.perf_counter` |
| 中对话 5轮10工具 | 流式 `appendStreamToken` 帧率 | `run.log: [perf]` + JS `console.time` |
| 长对话 25轮多代码 | 收尾 `finalize` 主线程时长 | Python 埋点 P95 |
| 50轮历史 | DOM 节点数 `$$('.msg-row').length` / RSS | `ps rss` + inspector |
| 功能回归 | 复制/表格滚动/公式/工具展开/回滚/重试/切换 | 手动 + `venv/bin/python3 -m unittest discover tests` |

## 6. 风险与回滚

* **回滚开关**：Phase 1B/2A/3A 各留 `AISettingsStore.enable_incremental_*/enable_async_markdown` 布尔，默认开，异常一键关。
* **WebKit 单例**：勿动 `ai_engine/ai_html_template.py:288 get_shared_web_context`。
* **线程安全**：`markdown` 非线程安全必须加锁；`GLib.idle_add` 仅主线程触 GTK/WebView。
* **测试**：`tests/test_ai_text_utils.py`, `test_webview_reload_guard.py`, `test_session_store.py` 必过。

## 7. 交付物

* 本方案 `docs/plans/ai-chat-render-optimization-phases.md`
* Phase 0 产 `docs/plans/ai-chat-render-perf-baseline.md`（基线表）
* 每阶段 PR 标题 `perf(ai-chat): phaseN-xxx`，附前后 `run.log` 采样与 inspector 截图。
