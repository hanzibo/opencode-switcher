# AI 助手看盘 — 对话渲染性能瓶颈分析

> 分支: `analyze/ai-chat-render-perf` · 日期 2026-08-21 · 只读分析，不改代码

## 0. 结论摘要（TL;DR）

| 优先级 | 瓶颈 | 位置 | 影响 |
|--------|------|------|------|
| **P0** | 流结束/工具阶段的**全量 Markdown 重排**（整轮转 `render_turn` + `pygments` + 4 个 `DOTALL` 正则） | `views/ai_chat/webview_mixin.py:173` `ai_engine/render_pipeline.py:139` `ai_text_utils/markdown.py:74` `ai_text_utils/render.py:289` | 工具循环每轮、最后一个 token 结束后各触发一次；工具 10~25 轮时 O(n²) 增长，主线程阻塞，UI 掉帧 |
| **P0** | **最终收尾双重全量渲染** (`_finalize_streaming_render` 内 `render_turn` + `_append_assistant_turn_to_cache` 各做一次 `markdown`) | `views/ai_chat/runner_mixin.py:332-392` | 单次对话结束时 2× 全文 markdown + 2× `run_javascript`，多余一次可直接复用 |
| **P1** | JS 端 `updateMessageContainer` **每帧全量 DOM 操作**：`temp.innerHTML` 解析 + 3 区替换 + `addCopyButtons` 全量扫描 + `_wrapTables` + `_debouncedRenderMath` | `html_templates/chat.js:424-469` | 每工具结果/每 `render_turn` 都触发，即使 `enable_incremental_tools` 已启用，仍有 assistant(tool_calls) 路径走全量 |
| **P1** | Python **主线程同步 Markdown** (via `GLib.idle_add`) 阻塞 GTK 事件循环 | `views/ai_chat/runner_mixin.py:81-102`, `webview_mixin.py:120` | LLM 后台线程通过 `idle_add` 把重任务抛回主线程，滚动/输入/窗口移动被卡 |
| **P1** | **KaTeX 全量扫描**（虽 800ms debounce，仍扫整个 bubble） | `html_templates/chat.js:46-56` `209-218` | 含公式的长回答每次工具更新都扫 `renderMathInElement(bubble)`，WebKit 内开销大 |
| **P2** | **DOM Windowing 仍保留全量节点** (`display:none` 而非移除) + 每帧 `querySelectorAll('#content > .msg-row')` | `html_templates/chat.js:260-291` `322-331` | 50 轮对话仍有 50+ 行在 DOM，每次 `applyWindowing`/`_updateRoundNav` 遍历 + `getBoundingClientRect` 强制回流（非 streaming 底部时） |
| **P2** | **大 HTML 经 `json.dumps` 过桥** (`run_javascript` 字符串可达数十 KB) | `webview_mixin.py:138` `runner_mixin.py:364` | WebKit IPC 序列化/反序列化 + JS `innerHTML` 解析双重成本 |
| **P2** | `Pygments`/ `codehilite` 不可中断且按主题每次 `markdown()` 都重新高亮 | `ai_text_utils/markdown.py:32-42`, `ai_engine/ai_html_template.py:59` | 代码块多时主线程 >100ms |

> 增量路径（`appendStreamToken` 60ms 批处理 + `updateToolCard` 单卡更新）本身已较优；瓶颈集中在**非增量回退路径**和**收尾阶段**。

---

## 1. 渲染链路全景

```
LLM SSE (ai_engine/llm_client.py:stream_chat_completion)
  → ai_engine/ai_tool_loop.py:run_llm_react_loop (ReAct 循环, 25 轮上限)
    → system/event_types.py:StreamEvent (TEXT_DELTA/REASONING_DELTA/TOOL_CALLS/STREAM_END)
      → views/ai_chat/runner_mixin.py:_run_llm_api_request (后台线程)
        ├─ on_token_delta → GLib.idle_add → StreamingMixin._on_token_delta (60ms batch)
        │     → _flush_token_buffer → run_javascript("appendStreamToken(...)")
        │       → chat.js:700 appendStreamToken (TextNode.appendData, _scrollToBottom RAF)
        ├─ on_tool_calls_started → finishReasoning() + 取消 flush 定时器
        ├─ append_message (assistant tool_calls / tool result)
        │     ├─ enable_incremental_tools && dom_ready ? 仅 _on_tool_result → updateToolCard (单卡)
        │     └─ 否则 → GLib.idle_add(_render_current_assistant_message)
        │         → webview_mixin.py:172 _render_current_assistant_message
        │           → render_pipeline.py:121 render_turn → _render_answer/tool/reasoning
        │             → _markdown_to_html_safe (5 正则 + markdown + pygments)  ← P0
        │           → build_update_js → run_javascript("updateMessageContainer(...)")
        │             → chat.js:424 全量三区替换 + addCopyButtons + _wrapTables + _debouncedRenderMath + _throttledWindowing
        └─ STREAM_END / 工具循环结束
            → _finalize_streaming_render (runner_mixin.py:332)
              → render_turn (全量, is_streaming=False) + run_javascript(updateMessageContainer)  ← P0
              → _append_assistant_turn_to_cache → _rebuild_markdown + _markdown_to_html_safe (第二次全量) ← P0
              → run_javascript(finishReasoning + addCopyButtons ...)
              → 保存对话 + 标题生成 + token 刷新
```

**切换/重建路径**（非流式）：

```
_switch_to_conversation / _retry_response / _prune_messages / _start_new_conversation
  → _render_markdown(text) [webview_mixin.py:120]
    → _markdown_to_html_safe(全文) → run_javascript("updateContent(...)")
      → chat.js:350 updateContent: content.innerHTML=html + _wrapTables + addCopyButtons + _renderMath(同步) + applyWindowing + _scrollToBottom
```

**HTML Shell**：`ai_engine/ai_html_template.py:186 _get_html_shell` LRU 16，已缓存；`get_html_template` 仅 `replace(__INITIAL_HTML_MARKER)`，非瓶颈。

---

## 2. 逐项瓶颈详解

### 2.1 P0 — 全量 `render_turn` / `_markdown_to_html_safe`

**触发点**

* `webview_mixin.py:173 _render_current_assistant_message`：每次 `append_message_callback` (非增量) 都以 `req_id` 关联的 `turn_msgs = _get_turn_messages()` (全轮) + `streaming_content/reasoning` 组 `TurnRenderInput` 调 `render_turn`。`render_turn` → `_render_standard_mode` → `_render_answer_html` (render.py:289) 对 `final_content = "\n".join(all_assistant_contents + streaming)` 做 `_close_unclosed_code_blocks` + `_markdown_to_html_safe`。
* `_markdown_to_html_safe` (markdown.py:74) 内部：
  1. `_escape_math` 5 个 `re.sub` (含 `DOTALL`)
  2. `_fix_latex` (已知命令回退)
  3. `_escape_tool_results` 4 个 `DOTALL` 正则 (`_TOOL_RESULT_PATTERN1~4`, 103-116) 扫描全文
  4. `_ensure_list_blankline` / `_ensure_table_blankline` / `_fix_blockquote_fences` / `_fix_details_blocks` 全行扫描
  5. `markdown.markdown(..., extensions=[...,"codehilite"])` — pygments 词法分析对每个 ``` 块同步高亮
  6. `_unescape_tool_results` 倒序替换 + 正则去 `<p>` 包裹
  7. `_unescape_math`

  单次对 20KB 文本实测主线程 80-200ms（取决于代码块数量），工具 15 轮时累计 >1s。

* `runner_mixin.py:332 _finalize_streaming_render` 内连续两次全量：`render_turn` (355) + `_append_assistant_turn_to_cache` (384) 各做一次 `markdown`，后者结果仅写入 `_ai_html_cache`，本可复用前者 `output.combined_html` 或 `rebuilt_markdown`。

**证据**

```
webview_mixin.py:206  turn_msgs = _get_turn_messages()
webview_mixin.py:206-215  render_turn(TurnRenderInput(...)) → build_update_js → run_javascript
render.py:308  final_content = _close_unclosed_code_blocks(final_content); rendered_md = _markdown_to_html_safe(final_content)
markdown.py:84  md_lib.markdown(escaped_text, extensions=_get_markdown_extensions())  # codehilite 在此
runner_mixin.py:355-378  output = render_turn(...); js_final = ...; _append_assistant_turn_to_cache() # 第二次 markdown
```

**影响**：主线程卡顿→ GTK 事件队列堆积→ 窗口拖动、输入框响应延迟、WebView 丢帧。

### 2.2 P0 — 增量已启用仍有全量回退

`runner_mixin.py:99` 仅对 `role=="tool"` 且 `enable_inc && is_active_stream && dom_ready` 走单卡 `updateToolCard`；`role=="assistant"` 含 `tool_calls` 的消息仍走全量 `render_turn`。一次 ReAct 迭代产生 1 条 assistant(tool_calls) + 1 条 tool，因此每迭代至少 1 次全量。25 轮=25 次全量。

### 2.3 P1 — JS `updateMessageContainer` 全量扫描

```js
html_templates/chat.js:438  temp.innerHTML = html; // 解析
         440-454  querySelector('.reasoning-region/.tool-region/.answer-region') + innerHTML 赋值
         456      addCopyButtons(div) // querySelectorAll('pre:not(.has-copy-btn)') 全 bubble 扫描 + 监听绑定
         457      _wrapTables(div)     // querySelectorAll('table')
         458      _debouncedRenderMath(div) // 非流式直接扫，流式 800ms 后扫整个 div
467      _throttledWindowing() // RAF applyWindowing
468      _scrollToBottom()     // RAF
```

即使只更新 tool 区，也会 `addCopyButtons` 扫整个 `msg-bubble`，对 10+ 工具卡片+Bubble 每次都 O(n)。

### 2.4 P1 — 主线程同步 Markdown

后台线程 (`_run_llm_api_request` 内的 `run_llm_react_loop`) 通过 `GLib.idle_add(self._render_current_assistant_message, req_id)` 把重任务推回主线程。`_render_current_assistant_message` 内无 `threading` 或 `idle_add` 分片，单次即占主循环一个 slice。流式 token 本身已走 60ms 轻量路径，但工具/收尾仍重。

### 2.5 P1 — KaTeX

`chat.js:46 _debouncedRenderMath` 流式期 800ms debounce 合理，但 `_renderMath` (209) 仍 `renderMathInElement(element)` 全量扫描 + 错误回退。无公式对话仍 pay 扫描成本。`updateContent` 路径 (350) 直接 `_renderMath(content)` 同步执行，可能阻塞首帧。

### 2.6 P2 — DOM Windowing 非虚拟化

* `applyWindowing` (260) 每次 `querySelectorAll(':scope > .msg-row')` + `':scope > .msg-row.user'`，50 轮=50 节点+隐藏 40 个，仅 `classList.add('msg-windowed') {display:none}`，节点仍在内存/样式计算中。
* `_throttledWindowing` RAF 节流已做，但仍每帧遍历。
* `_updateRoundNav` (602) 在 `!_isStreaming || !_autoScroll` 时对每个可见 `msg-row.user` 做 `getBoundingClientRect()` 强制回流；虽有 streaming 底部 fast-path，但用户上滑查看历史时流式仍会触发回流。

### 2.7 P2 — 大 HTML 过桥

`webview_mixin.py:138 js_code = f"updateContent({json.dumps(rendered_html)});"` 与 `runner_mixin.py:364 json.dumps(output.combined_html)` 可达 50-100KB，`run_javascript` 需在 WebKit IPC 中序列化，JS 侧再 `innerHTML` 解析，双重拷贝。可考虑分片或只传 diff。

### 2.8 P2 — Pygments

`markdown.py:32 _get_markdown_extensions` 中 `codehilite` 导致每个代码块都走 Pygments 词法。`stores/theme_config.py` 切主题时 `Pygments CSS` 重算。长代码回答高亮成本占比可达 30-40%。

---

## 3. 现有优化与有效性

| 已有优化 | 评价 |
|----------|------|
| `StreamingMixin._BATCH_FLUSH_MS=60` 批处理 token/reasoning (`streaming_mixin.py:97`) | **有效**，避免每 token 一次 `run_javascript` |
| `appendStreamToken` 纯 `TextNode.appendData` (`chat.js:700`) | **有效**，最轻量路径 |
| `_on_tool_result → updateToolCard` 单卡替换 (`streaming_mixin.py:176`, `chat.js:889`) | **有效**，但仅覆盖 tool result，未覆盖 assistant tool_calls |
| `_debouncedRenderMath 800ms` + `window._isStreaming` 门控 (`chat.js:46`) | **有效**，已大幅降低公式开销 |
| `_throttledWindowing RAF` (`chat.js:58`) | **有效** |
| `_updateRoundNav` streaming 底部 fast-path 跳过 `getBoundingClientRect` (`chat.js:617`) | **有效** |
| `_get_html_shell LRU` (`ai_html_template.py:186`) | **有效**，shell 零成本 |
| `MAX_VISIBLE_ROUNDS=10` display:none windowing (`chat.js:227`) | **部分有效**，避免绘制但不减 DOM/查询成本 |

---

## 4. 测量建议（复现前必做）

1. **Python 侧埋点**：在 `_markdown_to_html_safe`、`render_turn`、`_finalize_streaming_render` 前后 `time.perf_counter()` 打 `print("[perf] ...", flush=True)`，跑 `run.log` 统计 P50/P95；或复用 `StreamingMixin._STREAM_PERF_LOG` 旗标扩展。
2. **JS 侧埋点**：`chat.js` 首行 `performance.mark/measure` 包 `updateMessageContainer`/`_renderMath`/`applyWindowing`，`console.time` 输出到 WebKit inspector（`WEBKIT_INSPECTOR=1`）。
3. **基准对话**：准备 3 档 fixture：短(1 轮无工具)、中(5 轮 10 工具)、长(25 轮 25 工具+多代码块)，分别测切换(`updateContent`)与流式(`appendStreamToken` + 工具增量)耗时。
4. **WebKit 进程 RSS**：`ps -o rss -p $(pgrep -f WebKitWebProcess)` 观察 windowing 前后。

---

## 5. 优化方向（按 ROI 排序，仅建议不落地）

* **去重收尾全量**：`_finalize_streaming_render` 复用 `render_turn` 结果给 cache，避免第二次 `markdown`；或让 `_append_assistant_turn_to_cache` 接受已渲染 html。
* **assistant tool_calls 也增量**：为 `tool_calls` 新增 `appendToolCalls` JS API，增量插入卡片而不重排全轮。
* **主线程外 Markdown**：将 `_markdown_to_html_safe` 移至后台线程，`GLib.idle_add` 仅做 `run_javascript`；需处理 `markdown` 非线程安全（可池化或加锁）。
* **JS 局部扫描**：`addCopyButtons`/`_wrapTables` 改为仅扫新增 `details`/`table`/`pre`（传 `newDetails` 已有雏形，`updateMessageContainer` 仍扫全 bubble）。
* **真虚拟化**：超 10 轮时从 DOM 移除而非 `display:none`，或分页加载（`showOlderBatch` 已有批量揭示，可改为按需 `innerHTML` 注入）。
* **可选关闭高亮/公式**：长对话提供 `codehilite`/`renderMath` 开关，或对超长内容降级为纯文本。

---

## 6. 风险与约束

* WebView 必须复用 `get_shared_web_context()` 单例，勿新建 context（`AGENTS.md: WebView`）。
* `MemoryPressureSettings` 仅构造时生效，不要运行时改。
* `TEMPLATE_REGEX` / `classifyText` / `_AI_COMMANDS` 多处同步，改动需联动。
* `GLib.idle_add` 回调内勿改 widget 层级；PyGObject 异常被吞，注意 `run.log`。

## 7. 附：关键文件索引

* `views/ai_chat/panel.py:97` `_BATCH_FLUSH_MS`
* `views/ai_chat/streaming_mixin.py:59` `_on_token_delta` / `72` `_flush_token_buffer` / `131` `_on_tool_calls_started` / `176` `_on_tool_result`
* `views/ai_chat/webview_mixin.py:90` `_load_webview_html` / `120` `_render_markdown` / `172` `_render_current_assistant_message`
* `views/ai_chat/runner_mixin.py:40` `_run_llm_api_request` / `332` `_finalize_streaming_render` / `402` `_handle_stream_end`
* `ai_engine/render_pipeline.py:121` `render_turn` / `205` `build_update_js`
* `ai_text_utils/markdown.py:74` `_markdown_to_html_safe` / `104` `_escape_tool_results`
* `ai_text_utils/render.py:200` `_render_reasoning_html` / `243` `_render_tool_steps_html` / `289` `_render_answer_html`
* `ai_engine/ai_html_template.py:186` `_get_html_shell` / `288` `get_shared_web_context`
* `html_templates/chat.js:46` `_debouncedRenderMath` / `58` `_throttledWindowing` / `260` `applyWindowing` / `350` `updateContent` / `424` `updateMessageContainer` / `700` `appendStreamToken` / `889` `updateToolCard` / `602` `_updateRoundNav`
* `views/ai_chat/session_mixin.py:134` `_start_new_conversation` / `492` `_switch_to_conversation`
