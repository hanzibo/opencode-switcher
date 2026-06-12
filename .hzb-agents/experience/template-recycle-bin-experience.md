# 自定义分类模板回收站机制开发经验总结

> **分支名**：`add-feature-template-trash`  
> **开发周期**：2026-06-12 至 2026-06-12  
> **关键词**：`Recycle Bin` `Clipboard` `Template Delete` `GTK Dialog` `Focus Guards` `Transactional State`

## 一、经验与教训总结

### 1.1 做得好的地方
- **事务性状态设计**：通过在打开对话框时对 `self._cat_store._categories` 和 `self._cat_store._recycle_bin` 使用 `deepcopy`，让所有的新增、删除、清空、还原均只在临时内存副本中操作。这极大地简化了 UI 渲染刷新机制，并且在用户点击 “Cancel” 或关闭窗口时，能够干净且安全地丢弃所有修改，只有在点击 “Confirm” 时才落盘持久化。
- **动态分类重建规避冲突**：当用户从回收站还原模板时，若发现其原有分类已被用户彻底删除，系统会通过 UUID 重新创建一个新的分类，并且在内存中首先进行同名检测，避免引发分类名称唯一性校验冲突。
- **自定义 Window 的暗黑/亮色样式动态适配**：引入类名 `.custom-dialog` 给 `Gtk.Window`，并在应用动态主题注入阶段（`_set_theme`）扩充其相关背景色、文本色、按钮、滚动视口以及列表项的 CSS 定义，实现了无缝的主题无感切换。

### 1.2 需要改进的地方
- **嵌套对话框的焦点回调叠加**：在多层对话框（主 Recycle Bin 弹窗和子二次确认弹窗）中，子弹窗被销毁时若误调用 `on_dialog_hidden()`，会触发延迟 300ms 清除 `self._dialog_active` 状态的动作，进而导致当用户与未关闭的回收站主窗口交互时引发整个应用面板隐藏的故障。应当确保只有顶级（最外层）窗口的生命周期接管焦点标志状态。

## 二、关键问题与解决方案记录

### 问题1：二次确认弹窗销毁后，再次与回收站交互导致应用面板意外关闭
- **问题描述**：在回收站点击 "Permanently Delete All" 弹出二次确认弹窗，确认或取消该确认弹窗后，一旦再尝试点击回收站中其他项，应用的主面板就会因失去焦点而隐藏。
- **原因分析**：二次确认 MessageDialog 销毁时，在其 `response` 回调里误触发了 `self.on_dialog_hidden()`，从而开启了全局 `_dialog_active` 延迟清零的 300ms 定时器。当定时器触发后 `_dialog_active` 变为 `False`，使得主回收站弹窗处于无焦点防护状态，再次点击即触发主面板的 focus-out 隐藏。
- **解决过程**：
  1. 审查生命周期，发现回收站 Window 本身绑定了 `destroy` 信号，在关闭时才会调用 `on_dialog_hidden()`；
  2. 确认子确认对话框（MessageDialog）不需要单独更新 `_dialog_active` 状态，因为其父窗口依然处于显示状态。
- **最终方案**：从 MessageDialog 的 response 处理中删除了对 `self.on_dialog_hidden()` 的调用，保留主回收站 Window 对销毁状态的独占托管。
- **预防建议**：当弹窗中包含次级弹出层时，焦点防闪退机制（`_dialog_active` 状态）应且仅应由最外层主窗口的 `show`/`destroy` 信号全权托管。

### 问题2：暗黑主题下自定义弹窗显示白色背景且文本难辨
- **问题描述**：在 dark 主题下，新写的 Recycle Bin 以及原有的 Sort 对话框内部列表项和窗口背景均为系统默认的白色，并且按钮字样极其模糊。
- **原因分析**：该应用的暗黑样式选择器主要覆盖 `dialog`, `messagedialog`, `GtkDialog` 等节点，而 Sort 和 Recycle Bin 均是通过 `Gtk.Window.new(Gtk.WindowType.TOPLEVEL)` 创建的自定义顶级窗口，因无法匹配选择器而退化回了默认主题颜色。
- **解决过程**：
  1. 向 `dialog` 对象中引入类名 `dialog.get_style_context().add_class("custom-dialog")`；
  2. 将 CSS 中 `dialog`, `messagedialog` 的各处属性定义（包括 label, entry, button, textview, scrolledwindow 等）都加上 `.custom-dialog` 的匹配项；
  3. 为 `.custom-dialog` 专用的列表行 `list` 和 `row` 添加适配的主题底色和描边。
- **最终方案**：使用样式类对 `Gtk.Window` 进行样式映射，确保其在主题改变时重新计算颜色缓存，表现形式与 `Gtk.Dialog` 一致。
- **预防建议**：若自定义弹窗不从 `Gtk.Dialog` 派生，需保证其被赋予特定的 Style Class，并在应用的 CSS Provider 模板中提供其相关的配色适配。

## 三、技术要点沉淀

- **焦点防闪退模式**：GTK3 中多弹窗下的防面板失焦折叠标准模版：
  ```python
  dialog = Gtk.Window.new(Gtk.WindowType.TOPLEVEL)
  dialog.get_style_context().add_class("custom-dialog")
  dialog.connect("show", lambda *_: self.on_dialog_shown and self.on_dialog_shown())
  dialog.connect("destroy", lambda *_: self.on_dialog_hidden and self.on_dialog_hidden())
  ```
- **ListBoxRow 细线隔离效果 CSS 最佳实践**：
  ```css
  .custom-dialog list { background-color: %(input_bg)s; border: 1px solid %(input_border)s; border-radius: 6px; padding: 4px 0; }
  .custom-dialog row { background-color: transparent; color: %(text_fg)s; border-bottom: 1px solid %(input_border)s; }
  .custom-dialog row:last-child { border-bottom: none; }
  .custom-dialog row:hover { background-color: %(hover_bg)s; }
  .custom-dialog row:selected { background-color: %(sel_bg)s; color: %(text_fg)s; }
  ```

## 四、后续优化建议

- **回收站限额防护（FIFO）**：当前版本的回收站是无限容积的，如果用户删除上万个历史模板项可能会撑大 `categories.json` 的体积。后期可引入类似剪贴板历史的 FIFO 丢弃机制，控制最大留存个数（如 50 项）。

## 五、参考资料

- [Gtk3 Style Context & CSS Node Reference](https://docs.gtk.org/gtk3/css-properties.html)
