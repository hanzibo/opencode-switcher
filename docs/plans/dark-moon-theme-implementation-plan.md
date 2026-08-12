# 「紫月星云 (Dark Moon)」UI 主题实施计划书

## 1. 全量代码改动梳理 (Code Change Mapping)

### 1.1 依赖关系与架构逻辑
`opencode-switcher` 的主题系统以 `stores/theme_config.py` 为单一事实源 (Single Source of Truth)，通过 4 阶段联动起整个应用的界面风格：
1. **数据与持久化层** (`stores/theme_config.py`)：定义 `DARK_MOON` 色彩字典，管理 `config.json` 的 `"dark-moon"` 读取与保存。
2. **GTK 搜索与剪切板视图** (`views/panel.py`, `views/clipboard_panel.py`)：通过 `get_panel_css_vals("dark-moon")` 生成 GTK CSS Provider，控制窗口背景、搜索框圆角、列表行圆角与选中态。
3. **AI 聊天与 WebView 渲染层** (`views/ai_chat_panel.py`, `ai_engine/ai_html_template.py`, `html_templates/chat.css`)：通过 `get_web_css_vars("dark-moon")` 与 `get_ai_gtk_colors("dark-moon")` 注入 CSS 变量与圆角样式，实现气泡、代码块、思考过程、工具调用卡片的紫月润色视效。
4. **设置与系统菜单** (`dialogs/settings_dialog.py`, `main.py`)：在 Settings 对话框及系统托盘中追加「紫月星云 (Dark Moon)」单选选项，支持实时无缝切换。

---

### 1.2 改动文件与代码清单

| 序号 | 目标文件路径 | 涉及类 / 函数 | 改动内容与伪代码说明 | 潜在影响/依赖 |
| :--- | :--- | :--- | :--- | :--- |
| **1** | [`stores/theme_config.py`](file:///home/hzb/opencode-switcher/stores/theme_config.py) | `DARK_MOON`<br>`_THEMES`<br>`get_theme()`<br>`load_theme_config()` | 1. 新增 `DARK_MOON` 配色字典（紫晶玄黑 `#0f0914`、紫罗兰高亮 `#c084fc`、月粉 `#f472b6`）；<br>2. 注册 `"dark-moon"` 至 `_THEMES`；<br>3. 更新 `load_theme_config()` 校验与默认回退逻辑。 | 作为全应用 Theme 核心数据源，无破坏性影响。 |
| **2** | [`views/panel.py`](file:///home/hzb/opencode-switcher/views/panel.py) | `SearchPanel._set_theme()` | 1. 调整 GTK CSS 模板中的圆角规则：<br>  - `#searchEntry` 圆角从 `8px` 提升至 `14px`；<br>  - `.row` 列表项圆角从 `6px` 提升至 `12px`；<br>2. 引入渐变微光边框与过渡阴影样式。 | 提高主搜索面板的视觉圆润感与现代质感。 |
| **3** | [`views/clipboard_panel.py`](file:///home/hzb/opencode-switcher/views/clipboard_panel.py) | `ClipboardPanel._apply_styles()` | 1. `.cat-row` / `.row` 圆角由 `6px` 提升至 `12px`；<br>2. 顶部分类按钮与搜索框圆角提升至 `14px`；<br>3. 适配 `dark-moon` 主题动态色彩更新。 | 增强剪切板历史列表的视觉层次。 |
| **4** | [`views/ai_chat_panel.py`](file:///home/hzb/opencode-switcher/views/ai_chat_panel.py) | `AIChatPanel.set_theme()` | 1. 注入 `dark-moon` 的 GTK RGBA 背景色；<br>2. `_ai_entry` 消息输入框圆角提升至 `14px`；<br>3. 子代理状态条与 FlowBox 块样式圆角提升至 `12px`。 | 实现 GTK 输入框与 WebKit 视图的颜色及圆角一体化。 |
| **5** | [`html_templates/chat.css`](file:///home/hzb/opencode-switcher/html_templates/chat.css) | 聊天 WebView 全局样式 | 1. 提升各类组件圆角：<br>  - 代码块 `.code-block-container` / `pre`: `12px`；<br>  - 消息气泡 `.user-bubble`: `16px 16px 4px 16px` / `.assistant-bubble`: `16px`；<br>  - 思考折叠框 `.reasoning-container`: `12px`；<br>  - 工具卡片 `.tool-step-details`: `12px`；<br>2. 增加暗紫微光边框与阴影效果。 | 显著提升 WebKit 渲染区域的品质感与动效流畅度。 |
| **6** | [`dialogs/settings_dialog.py`](file:///home/hzb/opencode-switcher/dialogs/settings_dialog.py) | `_build_theme_tab()`<br>`_on_save()` | 1. 在主题 RadioButton 组中新增 `"紫月星云 (Dark Moon)"` 单选项；<br>2. 保存时支持持久化 `"dark-moon"` 并触发 `_on_theme_changed("dark-moon")`。 | 设置界面支持选择新主题。 |
| **7** | [`main.py`](file:///home/hzb/opencode-switcher/main.py) | `App._on_theme_changed()` | 1. 托盘菜单主题选择与启动时的主题加载逻辑支持 `"dark-moon"`。 | 系统级主题联动。 |
| **8** | `tests/test_theme_config.py` | `TestThemeConfig` | 1. 新增对 `"dark-moon"` 主题数据完整性、CSS 变量生成及加载的单元测试。 | 确保全量自动化测试套件 Pass。 |

---

## 2. 分步骤实施计划 (Step-by-Step Execution Plan)

### 阶段 1：Theme 数据模型与持久化 (`stores/theme_config.py`)
- **目标**：建立完整的 `DARK_MOON` 颜色配置，并支持主题持久化。
- **改动位置**：`stores/theme_config.py:79-140`
- **实现说明**：
  ```python
  DARK_MOON: Dict[str, Any] = {
      "panel_bg":          (0.059, 0.035, 0.078, 1.0),    # #0f0914
      "panel_title":       (0.96,  0.94,  0.98,  1.0),
      "panel_dir":         (0.66,  0.52,  0.78,  1.0),
      "panel_snippet":     (0.48,  0.38,  0.58,  1.0),
      "panel_separator":   (0.66,  0.33,  0.97,  0.08),
      "dot_live":          (0.659, 0.333, 0.969, 0.95),   # #a855f7
      "dot_recent":        (0.957, 0.447, 0.714, 0.85),   # #f472b6
      "dot_closed":        (0.48,  0.38,  0.58,  0.5),
      "window_border":     "rgba(168,85,247,0.18)",
      "hover_bg":          "rgba(168,85,247,0.07)",
      "sel_bg":            "rgba(168,85,247,0.14)",
      "sel_border":        "#c084fc",
      "search_bg":         "#181124",
      "search_fg":         "#faf5ff",
      "caret":             "#c084fc",
      "input_border":      "rgba(168,85,247,0.22)",
      "tab_fg":            "rgba(245,240,250,0.50)",
      "tab_active_fg":     "#ffffff",
      "dialog_bg":         "#0f0914",
      "text_fg":           "#faf5ff",
      "input_bg":          "#181124",
      "input_fg":          "#faf5ff",
      "btn_bg":            "rgba(168,85,247,0.08)",
      "btn_border":        "rgba(168,85,247,0.20)",
      "btn_hover":         "rgba(168,85,247,0.18)",
      "btn_active":        "rgba(168,85,247,0.28)",
      "ai_bg":             (0.059, 0.035, 0.078, 1.0),
      "ai_header_bg":      (0.094, 0.067, 0.141, 1.0),
      "ai_input_bg":       (0.094, 0.067, 0.141, 1.0),
      "web_bg":            "#0f0914",
      "web_text":          "rgba(250,245,255,0.95)",
      "web_pre_bg":        "#181124",
      "web_code_bg":       "rgba(168,85,247,0.12)",
      "web_code_fg":       "#f472b6",
      "web_pre_border":    "rgba(168,85,247,0.20)",
      "web_thinking":      "#c084fc",
      "web_answer":        "#f472b6",
      "web_user":          "#a855f7",
      "web_assistant":     "#e879f9",
      "web_table_header":  "rgba(168,85,247,0.12)",
      "web_table_alt":     "rgba(168,85,247,0.05)",
      "web_toggle":        "#c084fc",
  }
  ```

### 阶段 2：GTK Panel 与 圆角 CSS 优化 (`views/panel.py`, `views/clipboard_panel.py`, `views/ai_chat_panel.py`)
- **目标**：在各个 GTK View 模块中升级界面圆角规则，并引入微光流线边框。
- **改动说明**：
  - `views/panel.py`：#searchEntry `border-radius: 14px;`, `.row` `border-radius: 12px;`
  - `views/clipboard_panel.py`：`.cat-row` / `.row` `border-radius: 12px;`
  - `views/ai_chat_panel.py`：`_ai_entry` `border-radius: 14px;`

### 阶段 3：WebKit WebView CSS 圆角升级 (`html_templates/chat.css`)
- **目标**：提升 DOM 元素的圆角润度（12px~16px）与晶莹色彩。
- **改动说明**：
  - `.user-bubble` `border-radius: 16px 16px 4px 16px;`
  - `.assistant-bubble` `border-radius: 16px;`
  - `pre`, `.code-block-container` `border-radius: 12px;`
  - `.tool-step-details` `border-radius: 12px;`

### 阶段 4：设置界面 UI 与切换入口联动 (`dialogs/settings_dialog.py`, `main.py`)
- **目标**：在 Settings 窗口的 Theme 标签页中添加“紫月星云 (Dark Moon)”单选控件，支持一键保存与即时生效。
- **改动说明**：
  - 更新 `_build_theme_tab` 添加第三项主题 RadioButton："紫月星云 (Dark Moon)"。

### 阶段 5：单元测试与验证 (`tests/test_theme_config.py`)
- **目标**：编写测试用例，确保 `dark-moon` 色彩映射齐全、持久化可无缝加载、全量测试 Passing。

---

## 3. 预期风险与回退策略

- **风险**：GTK3 部分低版本主题引擎可能不支持过于复杂的阴影或圆角继承。
- **回退策略**：使用明确的标准 CSS 属性 `border-radius` 与 `background-color`，避免使用非标准供应商前缀；若写入失败可一键恢复 `DARK` 默认字典。
