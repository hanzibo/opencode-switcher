# Implementation Plan: Nebula Spinner (紫月星云) — Mini WebView in AI Header

## 背景

用户确认方案 ①：在 AI 面板标题栏（`ai_hdr`）中嵌入一个 ~36px 的微型 WebKit WebView，
用 Web CSS/SVG 动画渲染"紫月星云"加载指示器，替换现有 `Gtk.Spinner`。
先实现 dark-moon（紫色）主题，light/dark 后续迭代，**做好可扩展性**。

关键约束：
- 位置在 GTK 层 `ai_hdr`，不参与 WebView 内容滚动 → 不与消息气泡/头像重叠
- 保留 `self._ai_spinner` 属性名与 `show()/start()/stop()/hide()` 接口 → 现有 9 处调用点与测试零改动
- 主题色通过 `theme_config.py` 统一管理 → 扩展新主题只需填色值

## 改动文件清单

| # | 文件 | 类型 | 改动 |
|---|------|------|------|
| 1 | `stores/theme_config.py` | 修改 | 3 主题各加 5 个 `ai_spinner_*` 颜色键；新增 `get_ai_spinner_vars(name)` |
| 2 | `ai_engine/nebula_spinner.py` | **新增** | `NebulaSpinner(Gtk.Box)` 类 + SVG/CSS 模板 + HTML 生成 |
| 3 | `views/ai_chat_panel.py` | 修改 | import + `_build_ui()` 创建处替换 + `set_theme()` 联动 |
| 4 | `tests/test_nebula_spinner.py` | **新增** | HTML 生成 / 主题注入 / 接口兼容测试 |
| 5 | `AGENTS.md` | 修改(可选) | 模块地图补 `ai_engine/nebula_spinner.py` |

现有测试（`tests/test_ai_*.py` 中 7 处 `panel._ai_spinner = _FakeSpinner()`）**零改动**，
因 `NebulaSpinner` 提供相同接口 `start/stop/show/hide`。

---

## 1. `stores/theme_config.py`

### 1.1 三个主题 dict 各新增 5 个键（dark-moon 填最终值，light/dark 先填占位）

```python
# ── AI panel nebula spinner (ai_engine/nebula_spinner.py) ──
# dark-moon（紫月星云：亮紫→暗紫渐变）
"ai_spinner_crescent_a": "#e9d5ff",          # 月牙亮端
"ai_spinner_crescent_b": "#7c3aed",          # 月牙暗端
"ai_spinner_orbit":      "rgba(192,132,252,0.55)",  # 星轨虚线环
"ai_spinner_dust":       "#f0abfc",          # 星尘粒子
"ai_spinner_glow":       "#a855f7",          # 月牙发光色
# light：暂用同款（后续迭代调浅底对比色）
# dark： 暂用同款（后续迭代调）
```

### 1.2 新增函数（仿照 `get_web_css_vars` 模式）

```python
def get_ai_spinner_vars(name: str) -> dict:
    """Return CSS-variable dict for the nebula spinner mini WebView."""
    t = get_theme(name)
    return {
        "crescent_a": t["ai_spinner_crescent_a"],
        "crescent_b": t["ai_spinner_crescent_b"],
        "orbit":      t["ai_spinner_orbit"],
        "dust":       t["ai_spinner_dust"],
        "glow":       t["ai_spinner_glow"],
    }
```

**依赖**：无（theme_config 是叶子模块）。**影响**：新键不影响现有 `get_theme()` 消费者（按 key 取值）。

---

## 2. `ai_engine/nebula_spinner.py`（新增）

### 2.1 类结构

```python
class NebulaSpinner(Gtk.Box):
    """~36px 紫月星云加载指示器（微型 WebView + SVG/CSS 动画）。

    接口兼容原 Gtk.Spinner：show/start/stop/hide。
    """
    def __init__(self, theme="dark-moon", size=36):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self._theme = theme
        self._size = size
        self.set_size_request(size, size)
        self.set_no_show_all(True)
        self._build_webview()

    def _build_webview(self):
        # 共享 web context（无独立 WebKit 进程）
        ctx = get_shared_web_context()
        self._webview = WebKit2.WebView.new_with_context(ctx) if ctx else WebKit2.WebView.new()
        # 最小化设置：禁滚动/禁交互/禁媒体/禁上下文菜单
        s = self._webview.get_settings()
        s.set_enable_media(False); s.set_enable_webrtc(False); ...
        self._webview.set_size_request(self._size, self._size)
        self._webview.set_sensitive(False)          # 禁交互
        self._webview.connect("context-menu", lambda *_: True)
        self._webview.connect("decide-policy", lambda *_: False)  # 禁导航
        self.pack_start(self._webview, True, True, 0)
        self._load()

    def _load(self):
        self._webview.load_html(_build_spinner_html(self._theme), "file:///spinner")

    def set_theme(self, name):          # 主题切换 → 重新注入 CSS 变量
        self._theme = name
        self._load()

    # ── 接口兼容（原 Gtk.Spinner）──
    def start(self):  pass              # CSS 动画自动循环，无需手动 start
    def stop(self):   pass              # 隐藏由 GTK show/hide 控制
    def show(self):   Gtk.Box.show(self); self._webview.show()
    def hide(self):   Gtk.Box.hide(self)
```

### 2.2 HTML 模板（`_build_spinner_html(theme)` 返回完整 HTML）

```html
<!DOCTYPE html><html><head><meta charset="utf-8"><style>
  /* 主题变量注入：{crescent_a} 等被 get_ai_spinner_vars 替换 */
  body { margin:0; background: transparent; overflow: hidden; }
  .nebula { width:36px; height:36px; position:relative; }
  svg { width:100%; height:100%; }
  .crescent { animation: nebula-spin 1.4s cubic-bezier(.4,0,.2,1) infinite;
              transform-origin: 50% 50%; }
  .crescent path { fill: url(#crescentGrad);
                   filter: drop-shadow(0 0 3px {glow}); }
  .orbit { stroke: {orbit}; fill: none; stroke-dasharray: 3 3;
           animation: nebula-spin-rev 4.2s linear infinite, nebula-pulse 2.4s ease-in-out infinite;
           transform-origin: 50% 50%; }
  .dust { fill: {dust}; animation: nebula-flicker 2.4s ease-in-out infinite; }
  .dust.d2 { animation-delay: .6s; } .dust.d3 { animation-delay: 1.2s; }
  @keyframes nebula-spin    { to { transform: rotate(360deg); } }
  @keyframes nebula-spin-rev{ to { transform: rotate(-360deg); } }
  @keyframes nebula-pulse   { 0%,100% { opacity:.4; transform: scale(.95);}
                              50%     { opacity:.8; transform: scale(1.05);} }
  @keyframes nebula-flicker { 0%,100% { opacity:.2; } 50% { opacity:1; } }
</style></head><body>
<div class="nebula">
  <svg viewBox="0 0 48 48">
    <defs><linearGradient id="crescentGrad" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0%" stop-color="{crescent_a}"/>
      <stop offset="100%" stop-color="{crescent_b}"/>
    </linearGradient></defs>
    <g class="orbit"><circle cx="24" cy="24" r="21"/></g>
    <g class="crescent">
      <path d="M24 8 A16 16 0 1 0 24 40 A12 12 0 1 1 24 8 Z"/>  <!-- 月牙 -->
    </g>
    <circle class="dust d1" cx="8"  cy="12" r="1.4"/>
    <circle class="dust d2" cx="40" cy="20" r="1.1"/>
    <circle class="dust d3" cx="12" cy="38" r="1.6"/>
    <circle class="dust d1" cx="36" cy="34" r="1.0"/>
  </svg>
</div></body></html>
```

**关键点**：
- `body background: transparent` + 微型 WebView 自身 `set_background_color(透明)` → 但透明可能触发 alpha 合成问题，**若实机异常则改为页面背景 = 主题 `ai_header_bg` 不透明**（视觉与 header 一致，无透穿风险）。实现时默认透明，验证后定。
- 月牙 path 用 SVG 环形月牙（外圆 16 半径 + 内圆 12 半径裁剪），视觉为"缺角圆环"→ 更像月牙/星云核心。

**依赖**：`stores.theme_config.get_ai_spinner_vars`、`ai_engine.ai_html_template.get_shared_web_context`。
**影响**：新增模块，不影响现有 import 链（ai_engine 无 views 反向依赖）。

---

## 3. `views/ai_chat_panel.py`

### 3.1 import（L47 附近，`from ai_engine.render_pipeline import ...` 同一区域）

```python
from ai_engine.nebula_spinner import NebulaSpinner
```

### 3.2 `_build_ui()` L320-322 替换

```python
# 旧：
self._ai_spinner = Gtk.Spinner.new()
self._ai_spinner.set_no_show_all(True)
ai_hdr.pack_start(self._ai_spinner, False, False, 0)

# 新：
self._ai_spinner = NebulaSpinner(theme=self._theme, size=36)
self._ai_spinner.set_no_show_all(True)   # Gtk.Box 继承
ai_hdr.pack_start(self._ai_spinner, False, False, 0)
```

**9 处 `show()/start()`、8 处 `stop()/hide()` 调用点不变**（接口兼容）。

### 3.3 `set_theme()`（L5015 附近，`self._theme = name` 之后）

```python
if getattr(self, "_ai_spinner", None) is not None:
    try:
        self._ai_spinner.set_theme(name)
    except Exception:
        pass
```

**依赖**：NebulaSpinner。**影响**：`_ai_spinner` 从 `Gtk.Spinner` 变为 `Gtk.Box` 子类，`pack_start` 兼容；
`isinstance` 检查（若有）需注意——grep 确认无。WebView 崩溃重建路径不影响（微型 WebView 独立 widget）。

---

## 4. `tests/test_nebula_spinner.py`（新增）

```python
class TestNebulaSpinner(unittest.TestCase):
    def test_html_contains_theme_vars(self):      # dark-moon 色值注入
    def test_set_theme_reloads(self):            # set_theme 后变量更新
    def test_interface_compat(self):             # show/start/stop/hide 可调用
    def test_gtk_box_type(self):                 # 是 Gtk.Box（可 pack_start）
```

**说明**：`NebulaSpinner` 构造需要 GTK/WebKit 环境，测试用 `Gtk.init` 或 mock WebView（仿现有测试模式）。
现有 7 处 `_FakeSpinner` 替换测试**不改**（接口兼容）。

---

## 5. `AGENTS.md`（可选，提交时更新）

模块地图新增：`ai_engine/nebula_spinner.py`（NebulaSpinner 类，微型 WebView 加载指示器）。

---

## 实施顺序

| 步骤 | 目标 | 验证 |
|------|------|------|
| 1 | `theme_config.py` 加键 + `get_ai_spinner_vars()` | `py_compile` + 既有 theme 测试 |
| 2 | 新建 `ai_engine/nebula_spinner.py` | `py_compile` + 单测 |
| 3 | `ai_chat_panel.py` 接入 | `py_compile` + 既有 AI 测试（零改动通过） |
| 4 | 新增 `tests/test_nebula_spinner.py` | 单测通过 |
| 5 | 全量 `unittest discover`（624+） | 全绿 |
| 6 | 实机验证（见下） | 手动 |

## 手动验证清单

1. 启动应用 → 发送消息 → AI header 右上角出现 36px 紫月星云动画（月牙旋转 + 星轨反向呼吸 + 粒子闪烁）
2. 回答中滚动对话 → 确认 spinner **不随内容滚动、不与气泡/头像重叠**
3. 回答结束 → spinner 消失
4. 切换主题 dark-moon ↔ dark ↔ light → spinner 颜色跟随（dark-moon 紫色系；dark/light 暂同款，后续迭代）
5. 拖动分栏器/关闭重开面板 → 无透穿、无残留动画

## 回退策略

- 若微型 WebView 透明背景导致显示异常 → 页面背景改 `ai_header_bg` 不透明（改 1 处模板）
- 若微型 WebView 性能/稳定性问题 → 保留 `NebulaSpinner` 类，内部 `_build_webview` 回退为 `Gtk.Spinner`（接口不变，调用点零改动）
- 各文件改动相互独立，可逐文件 `git checkout` 回退
