# 自定义弹窗隐藏与模态挂起修复 开发经验总结

> **分支名**：`fix-feature-sort-dialog-unresponsive`  
> **开发周期**：2026-06-13 至 2026-06-13  
> **关键词**：`自定义窗口` `模态锁定` `transient` `界面挂起` `GTK3-CSS`

## 一、经验与教训总结

### 1.1 做得好的地方
- **精准的特征匹配隔离**：在 `hide()` 顶层清理逻辑中，通过检测 `Gtk.Window` 是否带有 `.custom-dialog` 样式类，成功将非 `Gtk.Dialog` 派生的自定义顶层窗口与 GTK 内部的单例窗口（如 `GtkTooltipWindow`）隔离开来，做到了精准匹配与安全销毁。
- **一致性对齐重构**：将 “Sort Categories” 弹窗底部的按钮边距与打包方式与 “Sort Items” 弹窗进行 100% 对齐重构，消除了布局设计上的缺陷，统一了组件封装和视觉规范。

### 1.2 需要改进的地方
- **漏设父子窗口绑定**：在最初编写 `_show_sort_dialog` 弹窗时，遗漏了设置 `dialog.set_transient_for(self.get_toplevel())`，导致窗口管理器和程序自身的清理逻辑无法溯源，暴露出开发和测试覆盖的盲区。

## 二、关键问题与解决方案记录

### 问题1：弹出类别排序窗口时隐藏面板，再次呼出面板导致卡死无响应
- **问题描述**：在 Clipboard 中打开 “Sort Categories” 弹窗后，通过快捷键强行隐藏面板，再次打开面板时，主面板失去所有交互响应，无法点击任何选项。
- **原因分析**：
  1. “Sort Categories” 使用 `Gtk.Window.new(Gtk.WindowType.TOPLEVEL)` 与 `dialog.set_modal(True)` 创建，属于带有模态特性的普通窗口，而非 `Gtk.Dialog` 派生。
  2. 主窗口在隐藏（`hide()`）时，仅对 `isinstance(win, Gtk.Dialog)` 的 transient 窗口进行销毁以防误杀 tooltip 等单例窗口。
  3. 自定义窗口由于类型不符逃过了清理，虽然随主面板隐藏但并未被销毁，并且仍然独占全局模态输入（Modal Grab）。
  4. 再次呼起面板时，残留的模态锁依然生效，屏蔽了主面板的所有交互输入，造成卡死。
- **解决过程**：
  修改 [panel.py: hide()](file:///home/hzb/opencode-switcher/panel.py#L447-L453) 中的清理条件：
  ```python
  is_dialog = isinstance(win, Gtk.Dialog)
  is_custom = isinstance(win, Gtk.Window) and win.get_style_context().has_class("custom-dialog")
  if (is_dialog or is_custom) and win.get_transient_for() == self._window:
  ```
- **最终方案**：通过匹配 `.custom-dialog` 样式类识别自定义顶级弹窗，在面板隐藏时同步执行销毁，释放模态锁定。
- **预防建议**：只要在程序中创建了 `dialog.set_modal(True)` 的非标准 `Gtk.Dialog` 弹窗，必须赋予其特定类名（如 `custom-dialog`）并在隐藏主面板的全局生命周期中进行显式销毁清理。

### 问题2：模板排序弹窗隐藏面板后，窗口管理器未将其视为子窗口且隐藏时未释放 modal 锁
- **问题描述**：在 Clipboard 中对特定模板进行 `Sort` 时隐藏面板，再次呼出后系统同样响应卡死。
- **原因分析**：模板排序窗口 `_show_sort_dialog` 在创建时漏设了 `dialog.set_transient_for(self.get_toplevel())`，导致 `win.get_transient_for() == self._window` 为 `False`，无法被清理逻辑获取。
- **解决过程**：
  在 `clipboard_panel.py` 第 `594` 行 `_show_sort_dialog()` 方法中添加：
  ```python
  dialog.set_transient_for(self.get_toplevel())
  ```
- **最终方案**：补全 transient parent 绑定，使窗口具备父子关联。
- **预防建议**：所有自定义弹窗在声明时，必须连接到父级顶层窗口 `transient_for`。

## 三、技术要点沉淀

- **非标准 modal 窗口的销毁防护模式**：
  ```python
  # 检测是否为拥有自定义样式的顶级弹窗窗口
  is_custom = isinstance(win, Gtk.Window) and win.get_style_context().has_class("custom-dialog")
  ```
- **ListBox/GtkBox 右下角对齐排版模式**：
  使用 `pack_end` 依次载入右侧元素（从右向左），并结合 `margin_end` 实现完美的右对齐：
  ```python
  bottom_box = Gtk.Box.new(Gtk.Orientation.HORIZONTAL, 8)
  bottom_box.set_margin_end(12)
  bottom_box.pack_end(confirm_btn, False, False, 0) # 处于最右
  bottom_box.pack_end(cancel_btn, False, False, 0)  # 处于 Confirm 左侧
  ```

## 四、后续优化建议

- **引入 Dialog 状态机自动释放器**：可构建一个全局的 Dialog 管理机制，当面板隐藏或状态切换时，由该管理机制集中分发销毁命令，降低散落在各个 UI 模块中手动绑定的逻辑依赖。

## 五、参考资料

- [AGENTS.md 中的 GTK C-level 崩盘防御](file:///home/hzb/opencode-switcher/AGENTS.md)
- [GtkWindow API 文档](https://docs.gtk.org/gtk3/class.Window.html)
