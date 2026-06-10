# 分类置顶与暗黑主题适配 开发经验总结

> **分支名**：`add-feature`  
> **开发周期**：2026-06-09 至 2026-06-10  
> **关键词**：`分类置顶` `暗黑主题` `GTK3-CSS` `右键菜单崩溃` `焦点保护`

## 一、经验与教训总结

### 1.1 做得好的地方
- **焦点状态双向绑定与防颤保护**：巧妙地利用 `_dialog_active` 状态与 `GLib.timeout_add` 微秒级延时，解决了模态对话框关闭到主窗口重新捕获焦点期间因临时 focus-out 导致整个面板被异常隐藏的问题。并将延迟时间从 3000ms 优化至 300ms，在防隐藏和快速隐藏（点击外部）之间取得了最佳体验平衡。
- **纯原生 GTK3 样式穿透**：未引入第三方库，仅通过为 GdkScreen 级别注入高特异性 CSS 选择器，彻底重绘了系统级的 `Gtk.Dialog`、`Gtk.MessageDialog` 以及 CSD 窗口标题栏（`headerbar`）和右键菜单（`Gtk.Menu`），使其在全局系统为亮色主题下依然能强行渲染成深色调，实现了极高完成度的暗黑模式视觉统一。

### 1.2 需要改进的地方
- **GTK CSS 解析器局限性认知**：在第一版样式重构中误用了 Web 端的 `!important` 声明，导致 GTK 3 CSS 引擎静默报错（Junk at end of value）并罢工，使得整个面板一度崩溃。后续应注意不同 UI 框架对 CSS 子集实现限度。
- **UI 销毁生命周期隐患**：在右键菜单的 `deactivate` 事件监听中直接使用 `GLib.timeout_add`，若生命周期中父组件提前被销毁可能会有垃圾回收相关的潜在隐患。尽管当前未发生错误，但应在未来的底层组件中将定时器引用加以管理。

---

## 二、关键问题与解决方案记录

### 问题1：右键弹出分类菜单时程序闪退或崩盘
- **问题描述**：在侧边栏对自定义类别进行右键操作以呼出 "Show at Top" 菜单时，程序频繁发生 Segmentation Fault 或界面无响应死锁。
- **原因分析**：
  1. **信号回调中销毁组件**：右键点击触发了 selection-change，这进一步调用了主面板的 `_rebuild()`，该方法会销毁并重建侧边栏的部分列表项。在 GTK 处理 `button-press-event` 信号的回调期间执行 `_rebuild()` 会直接破坏当前 C 层的信号发送源（Widget Tree），导致内存指针失效。
  2. **Wayland 指针错误**：使用了 `menu.popup_at_pointer(event)`，该方法在 Wayland 环境下对 transient 事件结构体的生命周期要求极高，容易产生指针悬空。
- **解决过程**：
  - 在 `_on_category_button` 菜单唤起处使用 `GLib.idle_add()`，将菜单显示和逻辑处理推迟到当前事件循环空闲时执行。
  - 使用更具兼容性的传统 `menu.popup(None, None, None, None, event.button, event.time)` 替换 `popup_at_pointer`。
  - 在 `_rebuild()` 逻辑中增加属性存在性检查与拦截，避免重构仍在构建中的组件树。
- **最终方案**：通过 `idle_add` 异步触发菜单弹出，并引入防销毁保护机制。
- **预防建议**：切忌在任何 GTK 事件响应回调（Signal Callback）中同步执行涉及 Widget 树结构性改动（如销毁、清空、大批量重建）的逻辑。凡涉及重构，一律使用 `GLib.idle_add` 调度到空闲帧执行。

### 问题2：深色主题下对话框标题栏（HeaderBar）及右键菜单背景呈现刺眼的系统白色
- **问题描述**：当 Linux 系统全局为浅色主题时，运行 switcher 暗黑模式时弹出的“创建/编辑提示词”对话框的标题栏、菜单背景以及文本标签下方显示为大片刺眼的白色区域。
- **原因分析**：
  1. **背景渐变图层覆盖**：GTK3 默认主题的 `headerbar` 使用了 `background-image`（渐变图层）而非 `background-color`，单写 `background-color` 无法覆盖渐变。
  2. **容器与子类继承**：GtkDialog 的内容区及 GtkMenu 的内嵌 Label 默认带有系统背景色，未声明 `transparent` 会造成背景割裂。
- **解决过程**：
  - 在 CSS 中为 `headerbar` 清空渐变：`background-image: none;`。
  - 运用通配符规则 `dialog headerbar *` 强行将标题栏内的所有子容器、按钮及标签背景设为 `transparent`，以消除文字背景暗块。
  - 为 `menu` 和 `menuitem` 补充高优先级的自定义样式，绑定 `btn_hover` 为菜单悬停高亮。
- **最终方案**：设计并在 [panel.py](file:///home/hzb/opencode-switcher/panel.py) 与 [clipboard_panel.py](file:///home/hzb/opencode-switcher/clipboard_panel.py) 中注入完善的 GTK 3 选择器样式表。
- **预防建议**：重绘 GTK3 系统组件时，必须使用 `background-image: none;` 和 `box-shadow: none;` 清理默认立体渐变。

---

## 三、技术要点沉淀

- **GTK3 CSS 无 `!important` 原则**：GTK3 的 CSS 引擎不支持 `!important`。若需提高规则优先级，必须编写更具体的 CSS 选择器链（如 `dialog headerbar.titlebar` 代替单独的 `headerbar`）。
- **ListBoxRow 状态隔离设计**：
  在生成带有分割线（`Gtk.Separator`）的占位行时，为了避免其干扰键盘上下导航和点击响应，在构建 `ListBoxRow` 时明确进行如下防干扰属性隔离：
  ```python
  sep_row = Gtk.ListBoxRow.new()
  sep_row.set_selectable(False)
  sep_row.set_activatable(False)
  sep_row.set_can_focus(False)
  ```
  同时在 `ListBox` 的选中回调中增加属性哨兵过滤：`if not hasattr(row, 'cat_id'): return`。

---

## 四、后续优化建议

- **CSS 代码集中化管理**：目前 `panel.py` 和 `clipboard_panel.py` 各自有一套 `vals` 与 CSS 样式表字符串，虽然保证了解耦性，但造成了约 40 行的样式模板重复。未来可抽离为单独的 `style.css` 文件在 `utils.py` 或特定样式模块中统一加载。
- **Wayland 下的对话框层级关系**：在 Wayland 窗口管理器中，模态对话框有时无法完美居中于父窗口之上。可在将来调研是否需要更高级 GdkWindow 瞬态位置配置。

---

## 五、参考资料

- [GTK+ 3 CSS Selector Documentation](https://docs.gtk.org/gtk3/css-properties.html)
- [Gtk.Menu API Reference](https://docs.gtk.org/gtk3/class.Menu.html)
