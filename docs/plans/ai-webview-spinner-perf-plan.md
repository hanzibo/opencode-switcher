# AI 助手 WebView 渲染与 Spinner 流畅度优化实施计划

## 一、 方案目标与背景
在 AI 助手对话过程中，顶部 Header 的 Nebula 星云 Spinner 在流式输出或思考状态时偶发微卡顿/掉帧。
经深度排查，主要瓶颈在于：
1. **SVG 滤镜与非合成图层**：`.crescent` 月牙弧在旋转动画中附带实时 `filter: drop-shadow`，且 Spinner 未独立成 GPU 合成图层，坐标系动态计算几何边界；
2. **强制同步布局（Layout Thrashing）**：流式 Token 追加（~60ms 批次）触发滚动时，`_updateRoundNav` 频繁对所有历史行调用 `getBoundingClientRect()` 测量视口距离；
3. **DOM 全局扫描冗余**：`addCopyButtons` 每次更新遍历整篇文档的 `<pre>` 标签。

本计划旨在通过**硬件加速隔离、消除强制回流、静态坐标系优化与局部 DOM 扫描**，彻底消除渲染卡顿，保证 60 FPS 流畅度。

---

## 二、 全量改动点梳理

| 文件路径 | 涉及函数 / 规则块 | 改动性质 | 具体修改内容说明 |
|---|---|---|---|
| `ai_engine/ai_html_template.py` | `_NEBULA_CSS` | 优化 | ① `#ai-header-spinner` 增加 `transform: translateZ(0); will-change: transform; contain: paint;` 独立图层隔离；<br>② 将旋转组件 `.crescent`、`.orbit`、`.orbit-ring` 的 `transform-origin` 固定为 `24px 24px`，去除动态 `fill-box` 几何查询；<br>③ 将动态 `filter: drop-shadow` 提升至静止外层容器，消除旋转帧的连续 CPU 高斯模糊光栅化。 |
| `html_templates/chat.css` | `#ai-header`, `#content` | 优化 | ① `#ai-header` 添加 `transform: translateZ(0)` 图层隔离；<br>② `#content` 添加 `transform: translateZ(0); will-change: scroll-position;` 提升滚动与绘制性能；<br>③ `#content.streaming-glow` 增加 `will-change: box-shadow, border-color;` 提示合成器优化。 |
| `html_templates/chat.js` | `_updateRoundNav` | 优化 | 增加流式快速分支：当 `window._isStreaming` 且 `_autoScroll` 为真时，直接将轮次锚定为最新一轮（`total`），**跳过全部行的 `getBoundingClientRect()` 循环**，彻底消除 Layout Thrashing。 |
| `html_templates/chat.js` | `addCopyButtons` | 优化 | 增加 `root` 局部根节点参数 `addCopyButtons(root)`，支持传入 `container` 或 `div`，避免长对话下遍历整个文档的 `<pre>` 标签。 |
| `html_templates/chat.js` | `updateMessageContainer` | 优化 | 在三区更新分支中调用 `addCopyButtons(div)`，实现局部精准挂载。 |
| `tests/test_nebula_spinner.py` | `TestHeaderShell` | 适配/扩充 | 更新对 `transform-origin: 24px 24px`、图层隔离与流式性能辅助逻辑的自动化断言检验。 |

---

## 三、 分步实施计划

### 步骤 1：优化 Spinner CSS 与 SVG 坐标系 (`ai_engine/ai_html_template.py`)
- **位置**：`ai_engine/ai_html_template.py:131-159`
- **改动说明**：
  - 给 `#ai-header-spinner` 添加图层提升：`transform: translateZ(0); will-change: transform; contain: paint;`
  - 将 `.crescent` 上的动态旋转中心从 `transform-box: fill-box; transform-origin: center;` 改为精确的像素中心 `transform-origin: 24px 24px;`
  - 将 `.crescent` 上的 `filter: drop-shadow` 迁移到静止的 `#ai-header-spinner` 上，使其在旋转时仅发生 GPU 旋转变换而不重复光栅化模糊蒙版。

### 步骤 2：优化 Header 与滚动容器图层合成 (`html_templates/chat.css`)
- **位置**：`html_templates/chat.css:18-61`
- **改动说明**：
  - 为 `#ai-header` 增加图层提升 `transform: translateZ(0);`，防止内容区更新触发 Header 重绘；
  - 为 `#content` 增加 `transform: translateZ(0); will-change: scroll-position;`；
  - 为 `#content.streaming-glow` 增加 `will-change: box-shadow, border-color;`。

### 步骤 3：消除流式 Token 追加时的强制同步回流 (`html_templates/chat.js`)
- **位置**：`html_templates/chat.js:599-637`
- **改动说明**：
  - 在 `_updateRoundNav()` 入口处增加快速判断：
    ```javascript
    if (window._isStreaming && _autoScroll) {
        var total = userRows.length;
        if (total <= 1) {
            nav.style.display = 'none';
            _lastRound = 0;
            _lastTotal = 0;
            return;
        }
        if (_lastTotal === total && _lastRound === total) return;
        _currentRound = total;
        _lastRound = total;
        _lastTotal = total;
        nav.style.display = 'flex';
        var indicator = document.getElementById('round-indicator');
        if (indicator) indicator.textContent = total + '/' + total;
        var prevBtn = document.getElementById('round-prev');
        var nextBtn = document.getElementById('round-next');
        if (prevBtn) prevBtn.disabled = total <= 1;
        if (nextBtn) nextBtn.disabled = true;
        return;
    }
    ```

### 步骤 4：局域化 `addCopyButtons` 扫描 (`html_templates/chat.js`)
- **位置**：`html_templates/chat.js:424-469`, `470-485`
- **改动说明**：
  - `function addCopyButtons(root)` 支持传入局部元素，默认回退至 `document`；
  - `updateMessageContainer` 传入当前的 `div` / `container`。

### 步骤 5：单元测试验证与全量回归
- 运行 `tests/test_nebula_spinner.py` 验证主题变量与 Shell 结构断言；
- 运行 `venv/bin/python3 -m unittest discover tests` 验证全量 690+ 单元测试全部通过。
