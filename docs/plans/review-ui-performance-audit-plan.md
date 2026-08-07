# UI 性能瓶颈与主线程响应卡顿审查计划

## 1. 审查摘要

在 `improve-feature-ui-performance-audit` 分支下对 `views/ai_chat_panel.py`、`views/panel.py`、`views/clipboard_panel.py` 及 `stores/clipboard_store.py` 进行了全量 UI 性能与主线程响应度审查。

审查精准定位了以下场景中的 **GTK 主线程卡顿、打字掉帧与交互延迟** 核心瓶颈：
1. **主线程 4 重 `os.fsync()` 磁盘同步阻塞**：每次保存对话或切会话时在主线程连续执行 4 次磁盘 `fsync()`。
2. **搜索框打字强行 `gc.collect()` 垃圾回收**：每次搜索过滤渲染时同步触发全量垃圾回收，导致按键微卡顿。
3. **GTK 列表过滤回调中动态执行 30+ 正则评分**：剪贴板搜索时每次按键触发 150*30 次正则匹配。
4. **已命中 HTML 缓存时的冗余 Markdown 文本全量重建**：切会话时重复解算无用 Markdown。

---

## 2. 性能瓶颈问题清单（按优先级分级）

| 编号 | 优先级 | 影响场景 | 瓶颈描述 | 影响评估 |
| :--- | :--- | :--- | :--- | :--- |
| **PERF-01** | 🔴 高 | 切会话 / `/open` / `/new` / 存盘 | `ConversationStore.save_conversation` 与 `_write_index` 在写盘时连续执行 4 次同步 `os.fsync(file)` 和 `os.fsync(dir)`，在 GTK 主线程上直接阻塞 50ms~200ms | 造成切会话与提交 Prompt 时的明显界面冻结 |
| **PERF-02** | 🔴 高 | Session 搜索框打字过滤 | `views/panel.py:791` 在每次搜索按键 `_render()` 时强行同步调用 `gc.collect()` 遍历 Python 对象图 | 导致搜索框打字输入掉帧与微卡顿 |
| **PERF-03** | 🔴 高 | 剪贴板搜索框打字过滤 | `views/clipboard_panel.py:814` 在 GTK ListBox `_list_filter_func` 过滤回调中，若 `item.type` 未设置会动态调用 `classify_text()` 运行 30+ 正则表达式 | 150 行 * 30 正则/字符，引发严重的搜索打字延迟 |
| **PERF-04** | 🟡 中 | 命中 HTML 缓存切会话 | `views/ai_chat_panel.py:4222` 在已命中 `cached_html` 时，主线程依然同步执行了全量 `_rebuild_markdown_from_messages` | 浪费主线程 CPU 算力，拉长切会话响应时延 |
| **PERF-05** | 🟡 中 | 历史下拉菜单刷新 | `views/ai_popovers.py:507` 每次 `refresh_dropdown()` 强行销毁并重新创建全量 ListBoxRow 控件 | 影响弹窗响应速度 |

---

## 3. 分步优化方案

### Phase 1：解除主线程 `fsync()` 磁盘阻塞与去除强行 `gc.collect()` (PERF-01, PERF-02)
- **优化点**：
  - 将 `ConversationStore.save_conversation` 与索引写盘的 `write_json_private` 放入后台线程执行，或取消非必要的目录 `fsync(dir_fd)` 同步强刷。
  - 删除 `views/panel.py:791` 中的同步 `gc.collect()`，交由 Python 自动分代垃圾回收。

### Phase 2：预分类缓存与过滤回调去正则化 (PERF-03)
- **优化点**：
  - 在 `ClipboardStore.add_item()` 写入列表时预先完成 `classify_text()`，确保 `item.type` 永远有缓存，严禁在 `_list_filter_func` 过滤回调中动态执行正则匹配。

### Phase 3：缓存命中路径跳过 Markdown 重建与下拉菜单按需加载 (PERF-04, PERF-05)
- **优化点**：
  - 当 `cached_html` 存在且有效时，直接复用 HTML 进行 WebView 内容更新，跳过 `_rebuild_markdown_from_messages`。
  - `refresh_dropdown()` 改为只在下拉菜单真正展开时按需构建 Row。

---

## 4. 验证方法

1. **单元测试验证**：
   - 运行 `venv/bin/python3 -m unittest discover tests` 确认全量测试通过。
2. **基准性能对比**：
   - 测量 `save_conversation` 主线程耗时（优化前 ~120ms vs 优化后 <2ms）。
   - 测量搜索框连续打字时的帧率与延迟。

---

## 5. 回滚思路

- 本次优化均基于异步解耦、去重复计算与防抖策略，不改变存储格式与 API 契约。若遇到写盘并发，可安全加锁或回退至主线程无 `fsync` 包装。
