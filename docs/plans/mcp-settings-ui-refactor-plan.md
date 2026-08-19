# MCP 设置界面左右分栏 (Master-Detail) 重构实施计划

## 一、 改动目标与架构设计

重构 `dialogs/settings_dialog.py` 中的 MCP 服务器配置界面，将原本的“单列垂直滚动卡片列表”升级为现代化的“左侧列表概览 + 右侧详情编辑”双栏（Master-Detail）布局。

### 核心收益：
1. **零滚轮疲劳**：左侧一览所有 MCP 服务状态与类型，无需在长页面中上下翻滚寻找；
2. **快捷添加**：添加按钮固定在左侧列表顶部，随时可一键新增；
3. **状态即时联动**：右侧表单修改名称、类型或开关时，左侧条目即时联动刷新指示灯、标题与徽标；
4. **无损切换**：各服务器表单生命周期独立，左右切换时未保存的输入与测试日志不丢失；
5. **数据模型兼容**：严格保持与 `AISettingsStore.mcp_servers` 及 `MCPServerConfig` 的读写兼容性。

---

## 二、 涉及文件与改动梳理

| 文件路径 | 涉及函数 / 类 / 组件 | 改动性质 | 改动说明 |
|---|---|---|---|
| `dialogs/settings_dialog.py` | `SettingsDialog._build_mcp_tab` | 重构 | 重建 MCP 标签页容器为左右水平分栏布局（左侧导航 + 右侧 Stack 详情容器 + 空状态页） |
| `dialogs/settings_dialog.py` | `SettingsDialog._add_mcp_server_item` (原 `_add_mcp_server_card`) | 重构 | 创建左侧 `Gtk.ListBoxRow` 与右侧表单卡片，注册联动事件并装配入 `Gtk.Stack` |
| `dialogs/settings_dialog.py` | `SettingsDialog._update_mcp_list_row` | 新增 | 根据表单当前值（名称、启用状态、传输类型）实时刷新对应左侧行的 UI 标签与 Badge |
| `dialogs/settings_dialog.py` | `SettingsDialog._remove_mcp_server_item` | 重构 | 删除选中服务器，自动切换高亮相邻服务器或显示空状态页 |
| `dialogs/settings_dialog.py` | `SettingsDialog._create_mcp_empty_view` | 新增 | 创建“无 MCP 服务器”时的优雅占位引导界面 |
| `dialogs/settings_dialog.py` | `SettingsDialog._on_save` | 适配 | 保持遍历控件列表提取配置的逻辑，确保数据持久化无缝兼容 |
| `tests/test_mcp_phase3.py` | `TestMCPSettingsConfigParsing` | 扩充 | 增加对多服务器配置聚合与空列表场景的数据测试用例 |

---

## 三、 详细实施步骤

### 步骤 1：构建左右分栏主框架 (`_build_mcp_tab`)
- **位置**：`dialogs/settings_dialog.py:1276-1335`
- **内容**：
  1. 创建顶层水平容器 `Gtk.Box.new(Gtk.Orientation.HORIZONTAL, 0)`；
  2. **左侧侧边栏 (`vbox_left`)**：
     - 固定宽度 `220px`；
     - 顶部添加按钮：`＋ 添加 MCP 服务器`（全宽，醒目样式）；
     - 中间滚动容器：`Gtk.ScrolledWindow` 包含 `Gtk.ListBox`（`SelectionMode.SINGLE`）；
     - 底部辅助计数标签（如 `共 N 个服务器`）；
  3. **分割线**：`Gtk.Separator.new(Gtk.Orientation.VERTICAL)`；
  4. **右侧详情区 (`vbox_right`)**：
     - 使用 `Gtk.Stack`（带 `SLIDE_LEFT_RIGHT` 或 `CROSSFADE` 过渡）；
     - 页面 1：`empty_view`（空状态占位页）；
     - 页面 2..N：各 MCP 服务器的独立表单页面（置于各自的 `ScrolledWindow` 中）；
  5. 监听左侧 `listbox` 的 `row-selected` 信号，切换右侧 `stack` 显示对应服务器表单。

### 步骤 2：重构单服务器卡片创建与数据绑定 (`_add_mcp_server_item`)
- **位置**：`dialogs/settings_dialog.py:1345-1700`
- **内容**：
  1. 为每个服务器生成唯一 ID（如 `mcp_server_<uuid/seq>`）；
  2. **构建左侧 Row (`Gtk.ListBoxRow`)**：
     - 水平 Box：状态指示灯点（`●` 绿/灰） + 标题 Label (`Pango.EllipsizeMode.END`) + 徽标 Label (`stdio`/`http`)；
  3. **构建右侧表单 Detail Card**：
     - 划分 4 大结构卡片区域（基本信息与控制、传输协议切换、动态参数配置、测试操作栏）；
  4. **建立实时同步监听**：
     - `name_entry.connect("changed", ...)` → 实时更新左侧标题；
     - `enabled_switch.connect("notify::active", ...)` → 实时更新左侧状态灯；
     - `transport_combo.connect("changed", ...)` → 实时更新左侧徽标及右侧对应表单显隐；
  5. 将表单包装至 `Gtk.ScrolledWindow` 后 `stack.add_named(sw, server_id)`；
  6. 将数据控件字典存入 `self._mcp_server_widgets`。

### 步骤 3：实现删除与空状态切换逻辑 (`_remove_mcp_server_item`)
- **位置**：`dialogs/settings_dialog.py:1808-1825`
- **内容**：
  1. 从 `self._mcp_server_widgets` 移除对应记录；
  2. 从左侧 `listbox` 移除对应的 Row；
  3. 从右侧 `stack` 移除对应的 Form 容器；
  4. 若剩余服务器数量 > 0，自动选中第一项或上一项；若为空，`stack.set_visible_child_name("empty")`。

### 步骤 4：样式与视觉优化 (CSS)
- **位置**：`dialogs/settings_dialog.py` 内部样式注入
- **内容**：
  - 为左侧 ListBox Row 添加内边距、悬浮和选中高亮背景；
  - 为徽标添加圆角胶囊背景（Badge 样式，如 `background-color: rgba(66, 133, 244, 0.15); border-radius: 4px; padding: 2px 6px;`）；
  - 右侧表单各区域间距合理呼吸感。

### 步骤 5：单元测试验证与回归测试
- **位置**：`tests/test_mcp_phase3.py` 与全量测试套件
- **内容**：
  - 验证多 Server 配置与空配置序列化/反序列化；
  - 运行 `venv/bin/python3 -m unittest discover tests` 确保 689+ 用例全量通过。

---

## 四、 风险评估与回退策略

- **风险 1：GTK3 信号循环触发**
  - *应对*：在更新左侧 Row 标签时直接操作 `Label.set_text()`，不触发 ListBox 的重新选择信号，严格遵守 AGENTS.md 中的 signal safety 规范。
- **风险 2：删除当前选中项导致焦点丢失或 Crash**
  - *应对*：在移除组件前先将焦点转移，在 `GLib.idle_add` 中完成选区重定向。
