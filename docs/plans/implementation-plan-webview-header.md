# Implementation Plan: WebView 化 AI Header（去 GTK header + 省 160MB 进程）

## 背景与目标

微型 WebView spinner 会派生第二个 `WebKitWebProcess`（160+MB），不可接受。
方案：**删除 GTK `ai_hdr` 装饰区**，将 WebView 增高扩展到原 header 区域；
原 GTK header 的内容（标题/模型名/历史下拉/关闭按钮）与 spinner 全部迁移到
WebView 的 HTML 中（`#ai-header` 固定装饰区 + `#content` 独立滚动区）。
消息气泡仍在原滚动区内，永不与 header 重叠。

**关键设计决策（最小测试影响）**：保留 `_ai_spinner` / `_ai_lbl` / `_ai_history_popover`
三个属性名，替换为 **JS 桥对象**（接口兼容：show/start/stop/hide、set_markup、
refresh_dropdown/popdown/get_visible）→ 代码内 52 处调用点与 9 个测试文件 22 处
mock 引用**全部零改动**。

## 改动文件清单（8 个）

| # | 文件 | 类型 | 规模 |
|---|------|------|------|
| 1 | `ai_engine/ai_html_template.py` | 修改 | +~80 行：body 加 `#ai-header` 结构 + spinner SVG 内联 + 变量注入 |
| 2 | `html_templates/chat.css` | 修改 | +~60 行：flex 布局改造 + header 样式 |
| 3 | `html_templates/chat.js` | 修改 | ~15 处滚动迁移 + ~70 行 header 交互 |
| 4 | `views/ai_chat_panel.py` | 修改 | -150/+120 行：删 GTK header、新增桥对象与协议 |
| 5 | `views/ai_popovers.py` | 修改 | -350 行：删除 HistoryPopover 类 |
| 6 | `ai_engine/nebula_spinner.py` | **删除** | -177 行（SVG/CSS 并入主模板） |
| 7 | `tests/test_nebula_spinner.py` | 重写 | 改为测试模板 header 部分 |
| 8 | `AGENTS.md` | 修改(可选) | 模块地图更新 |

---

## 1. `ai_engine/ai_html_template.py`（模板层）

### 1.1 `_build_shell()`（L127-184）body 结构改造

```html
<body class="{theme_name}">
  <!-- 新增：固定装饰区（原 GTK header 迁移） -->
  <div id="ai-header" class="ai-header">
    <div id="ai-header-title" class="ai-header-title"><b>AI 助手看盘</b></div>
    <div id="ai-header-model" class="ai-header-model"></div>
    <button id="ai-history-btn" class="ai-header-btn" onclick="toggleHistoryDropdown()">历史对话 ▾</button>
    <div id="ai-header-spinner" class="ai-header-spinner" style="display:none">
      <!-- 紫月星云 SVG（从 nebula_spinner.py 迁移，变量 {crescent_a} 等注入） -->
    </div>
    <button id="ai-close-btn" class="ai-header-btn" onclick="closeAIPanel()">❌</button>
  </div>
  <!-- 新增：历史下拉面板 -->
  <div id="ai-history-dropdown" class="ai-history-dropdown" style="display:none">
    <div id="ai-history-list"></div>
    <div class="ai-history-actions">
      <button onclick="historyAction('clear')">清空已删除</button>
      <button onclick="historyAction('edit')">编辑</button>
    </div>
  </div>
  <div id="content">{_INITIAL_HTML_MARKER}</div>
  <!-- 现有 lightbox / round-nav 不变 -->
</body>
```

- spinner SVG：从 `nebula_spinner.py` 的 `_SPINNER_TEMPLATE` 迁移（`{crescent_a}` 等 5 个变量）
- CSS 变量注入：现有 `get_web_css_vars()` 循环之外，追加 `get_ai_spinner_vars()` 的 5 个变量
- **滚动结构由 chat.css 完成**（模板只加结构）

### 1.2 `get_html_template()`：不变（marker 替换逻辑与缓存键不变）

**依赖**：`stores.theme_config.get_ai_spinner_vars`（已存在）。**影响**：模板缓存键
不变（theme + pygments），header 随整页重建自动更新。

---

## 2. `html_templates/chat.css`（布局与样式）

### 2.1 滚动布局改造（关键）

```css
html, body { height: 100%; }                       /* 改现有 body 规则 */
body { display: flex; flex-direction: column; overflow: hidden; padding: 0; }
#ai-header { flex: none; height: 44px; display: flex; align-items: center;
             gap: 10px; padding: 0 10px;
             background-color: {bg_color}; }        /* 与消息区同色 */
#content  { flex: 1; overflow-y: auto; padding: 8px; }  /* 原 body padding 移到这里 */
```

⚠️ 现有 `body { padding: 8px; }` 改为 `#content { padding: 8px; }`，滚动从 body 移到 #content。

### 2.2 header 组件样式（+~40 行）

- `.ai-header-title`：粗体、`{text_color}`、flex:1 左侧
- `.ai-header-model`：小号次要色（`{text_color}` 70% 透明度）
- `.ai-header-btn`：扁平按钮（hover 背景）
- `.ai-header-spinner`：36×36 居中容器（SVG 尺寸 100%）
- `.ai-history-dropdown`：绝对定位面板（top:44px; right:10px; z-index:60; 背景 {bg_color} 边框圆角阴影）、列表行 hover/选中态

---

## 3. `html_templates/chat.js`（滚动迁移 + header 交互）

### 3.1 滚动迁移（约 15 处，重点回归风险）

| 行 | 现状 | 改为 |
|---|---|---|
| L233-234 | `window.addEventListener('scroll', ...)` `window.innerHeight + window.scrollY >= document.body.scrollHeight` | `content.addEventListener('scroll', ...)` `content.clientHeight + content.scrollTop >= content.scrollHeight` |
| L237-243 | `_scrollToBottom()`: `window.scrollTo(0, document.body.scrollHeight)` | `content.scrollTop = content.scrollHeight` |
| L559-585 | round 高亮：`window.scrollY`、`rect.top + window.scrollY` | `content.scrollTop`、`rect.top + content.scrollTop` |
| L596-612 | `_scrollToRound`/`_scrollToBottomForce`/`_scrollToTopForce`: `window.scrollTo` | `content.scrollTo(...)` |
| L837 | 初始 `_scrollToBottom()` | 不变（函数内部已改） |

统一：`var content = document.getElementById('content');`

### 3.2 header 交互 JS（+~70 行）

```javascript
function showHeaderSpinner()  { document.getElementById('ai-header-spinner').style.display = ''; }
function hideHeaderSpinner()  { document.getElementById('ai-header-spinner').style.display = 'none'; }
function updateHeaderTitle(title, model) {
  document.getElementById('ai-header-title').innerHTML = title;
  document.getElementById('ai-header-model').textContent = model || '';
}
function updateHistoryLabel(label) { document.getElementById('ai-history-btn').firstChild.textContent = label + ' ▾'; }
function closeAIPanel() { window.location = 'opencode://close-panel'; }
function toggleHistoryDropdown() {
  if (dropdown hidden) { window.location = 'opencode://history-open'; }  /* 请求数据 */
  else hide();
}
function renderHistoryList(itemsJson) { ... }   /* Python 通过 run_javascript 注入 */
function historySelect(id) { window.location = 'opencode://history-select?id=' + id; }
function historyDelete(id) { window.location = 'opencode://history-delete?id=' + id; }
function historyAction(kind) { window.location = 'opencode://history-' + kind; }
```

---

## 4. `views/ai_chat_panel.py`（核心）

### 4.1 删除（_build_ui L313-378 + 相关）

- ai_hdr 构建整块（`_ai_hdr`/`_ai_lbl`/`_ai_spinner`/`_ai_history_btn`/`_ai_history_btn_label`/btn_box/ai_close/on_ai_close_clicked/HistoryPopover 构造）
- `self.pack_start(ai_hdr, ...)` 与 `ai_sep_line`
- L452 `ai_hdr.override_background_color(...)`（set_theme 中 `_ai_hdr` 相关同步删除）
- L210-212 初始化 `_ai_history_popover/_ai_history_btn/_ai_history_btn_label = None` 改为桥对象

### 4.2 新增 3 个 JS 桥对象（保持接口兼容）

```python
class _HeaderSpinnerBridge:
    """spinner 控制 → run_javascript（兼容 show/start/stop/hide）"""
    def __init__(self, panel): self._panel = panel
    def show(self):  self._panel._run_header_js("showHeaderSpinner();")
    def hide(self):  self._panel._run_header_js("hideHeaderSpinner();")
    def start(self): pass
    def stop(self):  pass

class _HeaderTitleBridge:
    """标题更新 → run_javascript（兼容 set_markup）"""
    def __init__(self, panel): self._panel = panel
    def set_markup(self, markup): self._panel._run_header_js(f"updateHeaderTitle({json.dumps(markup)}, '');")

class _HistoryPopoverBridge:
    """历史下拉 → JS（兼容 refresh_dropdown/popdown/get_visible）"""
    def refresh_dropdown(self, edit_mode=False):
        self._panel._run_header_js("renderHistoryList(" + self._panel._history_summaries_json() + ");")
    def popdown(self): self._panel._run_header_js("hideHistoryDropdown();")
    def get_visible(self): return False   # GTK 可见性不再有状态，JS 侧自行管理
```

- `_run_header_js(js)`：封装 `run_javascript`（WebView 未 ready 时安全 no-op）
- `_history_summaries_json()`：调用 `_get_sorted_conversations()` 序列化（标题截断 25 字符、`(N条)` 格式对齐原 HistoryPopover.refresh_dropdown）
- 创建处（_build_ui）：`self._ai_spinner = _HeaderSpinnerBridge(self)` 等；52 处调用点不变

### 4.3 decide_policy 新增协议（L2300 后追加，仿 rollback-round 模式）

| 协议 | 处理 |
|---|---|
| `opencode://close-panel` | 原 on_ai_close_clicked 逻辑（hide + separator hide + queue_resize） |
| `opencode://history-open` | `run_javascript("renderHistoryList(<json>);")` + JS 显示下拉 |
| `opencode://history-select?id=` | 调 `_switch_to_conversation(id)` |
| `opencode://history-delete?id=` | 调现有删除对话逻辑（对齐 HistoryPopover `_on_delete_conversation_fn`） |
| `opencode://history-clear` | 调 `_reset_ai_panel_silent`（清空已删除） |
| `opencode://history-edit`（可选） | 二期：编辑模式（多选删除） |

### 4.4 其他修改

- `set_theme()`：删除 `_ai_spinner.set_theme(name)`（header 随整页重建自动跟随）
- import：删除 `from ai_engine.nebula_spinner import NebulaSpinner`；`HistoryPopover` import 删除（若仅此处用）
- `_apply_webview_gtk_background()`：保留（WebView 背景仍需）；其 docstring 中 header 相关描述更新

**依赖**：模板层（1/2/3）先完成。**影响**：decide_policy 是既有扩展点，新增协议低风险。

---

## 5. `views/ai_popovers.py`（删除 HistoryPopover）

- 删除 `class HistoryPopover`（L257-606，约 350 行）
- 检查 `_clean_history_title` 是否被其他类使用：若仅 HistoryPopover 用 → 一并删除；若复用 → 保留（HTML 版逻辑在 Python 侧 `_history_summaries_json` 中处理截断，可不再依赖）
- 保留 AICommandPopover 等其他类

---

## 6. `ai_engine/nebula_spinner.py`（删除）

- `_SPINNER_TEMPLATE`（SVG/CSS）→ 迁入 `ai_html_template.py`
- `_build_spinner_html()` → 并入模板构建（`get_ai_spinner_vars` 循环追加）
- `NebulaSpinner` 类删除
- `stores/theme_config.get_ai_spinner_vars()` **保留**（模板仍用）

## 7. `tests/test_nebula_spinner.py`（重写）

- 删除 NebulaSpinner 实例/接口测试
- 改为：`_build_shell()` 输出含 `#ai-header`、spinner SVG、5 个变量注入、`#content` 结构（纯函数断言）

## 8. `AGENTS.md`（可选）

- 模块地图：删 `ai_engine/nebula_spinner.py`；补"AI header 为 WebView 内 HTML"说明

---

## 实施顺序

| 步骤 | 目标 | 验证 |
|------|------|------|
| 1 | `ai_html_template.py`：header 结构 + spinner SVG + 变量注入 | py_compile + 模板单测 |
| 2 | `chat.css`：布局改造 + header 样式 | py_compile（CSS 无编译） |
| 3 | `chat.js`：滚动迁移 + header 交互 | 静态检查 + 实机 |
| 4 | `ai_chat_panel.py`：桥对象 + 删 GTK header + 新协议 | py_compile + 既有测试（应零改动通过） |
| 5 | `ai_popovers.py` 删 HistoryPopover；删 `nebula_spinner.py` | py_compile + grep 无残留引用 |
| 6 | 重写 `test_nebula_spinner.py` | 单测 |
| 7 | 全量测试（631+） | 全绿 |
| 8 | 实机验证（见下） | 手动 |

## 手动验证清单

1. **布局**：AI 面板顶部出现 HTML 装饰区（标题+模型名+历史+关闭+spinner 位），消息区从装饰区下方开始，**装饰区不随消息滚动**
2. **消息滚动**：长对话滚动 → 消息在装饰区下方滚动，**永不进入装饰区**；autoScroll 跟随、round-nav 跳转、复制按钮定位正常
3. **spinner**：发送消息 → 装饰区右侧紫月星云动画；回答结束消失；取消/错误也消失
4. **标题/模型名**：切换模型后显示更新；中止时显示"正在中止..."
5. **历史下拉**：点击"历史对话"→ 下拉列表（标题+N条）；选择切换对话；删除对话；清空已删除
6. **关闭按钮**：❌ 关闭 AI 面板（含 separator 隐藏、窗口 resize）
7. **主题切换**：light/dark/dark-moon → header 背景/文字/spinner 颜色全部跟随
8. **崩溃恢复**：WebView 崩溃重建后 header 功能正常
9. **进程检查**：`ps aux | grep WebKitWebProcess` → 仅 1 个 WebKit 进程（省 160MB）

## 回退策略

- 每步骤独立回退：`git checkout <file>` 单文件回退
- 整体回退：`git checkout master -- <改动文件>` 或 revert 提交
- 关键风险（滚动迁移）若回归严重：可先回退 chat.js 滚动迁移，header 固定区域暂用 `position: sticky` 过渡（接受轻微重叠），再单独修滚动
