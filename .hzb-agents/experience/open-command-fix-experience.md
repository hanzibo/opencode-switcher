# OpenCode Switcher /open 命令焦点失效修复 开发经验总结

> **分支名**：`fix-feature-open-command`  
> **开发周期**：2026-06-13 至 2026-06-13  
> **关键词**：`open命令` `文件选择器` `焦点丢失` `失焦折叠` `GTK3-Dialog`

## 一、经验与教训总结

### 1.1 做得好的地方
- **焦点防御模式的一致性复用**：直接复用了 `SearchPanel` 现有的 `_on_clip_dialog_shown` 与 `_on_clip_dialog_hidden` 焦点钩子，以最简的 2 行信号连接代码解决了窗口失焦折叠问题，保持了架构设计的一致性。
- **快速的缺陷定位与防抖机制审查**：通过分析 500ms 的防抖时间限制，合理解释了该缺陷为何在“快速回车”时正常、在“等待超过 500ms 后回车”时必现的偶发规律，精准锁定了窗口焦点转移与主面板 `_on_focus_out` 之间的时序竞争。

### 1.2 需要改进的地方
- **系统预置命令测试不足**：在开发初期，对 `/open` 这种需要调起外部 `transient` 文件选择器的内置指令缺乏深度边界测试，未提前防范对话框夺取焦点对主窗口失焦事件的影响。

## 二、关键问题与解决方案记录

### 问题1：输入 `/open` 并按回车后，弹出的目录选择对话框瞬间消失
- **问题描述**：在搜索栏输入 `/open` 并回车触发目录选择后，`Gtk.FileChooserDialog` 在弹出瞬间立即关闭，且搜索主面板也一同退出，无法进行目录选择。
- **原因分析**：
  1. 当 `Gtk.FileChooserDialog` 显现时，主窗口 `SearchPanel` 失去输入焦点，触发了底层 X11/Wayland 窗口管理器的 `focus-out-event`。
  2. 主窗口的 `_on_focus_out` 捕获失焦事件，检测到防抖时间差已超过 500ms，且焦点守护标识 `self._dialog_active` 为 `False`（未将该文件选择对话框纳入焦点管理），于是触发 `self.hide()`。
  3. `self.hide()` 包含清理逻辑，会隐式隐藏并销毁所有以主窗口为 `transient_for` 父窗口的 `Gtk.Dialog` 子类。新显示的 `Gtk.FileChooserDialog` 因而被主窗口误杀销毁。
- **解决过程**：
  在 `_do_select(self, session)` 中创建 `Gtk.FileChooserDialog` 后，注入以下信号监听：
  ```python
  dialog.connect("show", lambda *_: self._on_clip_dialog_shown())
  dialog.connect("destroy", lambda *_: self._on_clip_dialog_hidden())
  ```
- **最终方案**：通过连接对话框的 `show` 与 `destroy` 信号至 `SearchPanel` 的焦点防护钩子，在对话框生命周期内维持 `self._dialog_active = True`，从而阻断主窗口因失焦执行隐藏清理动作。
- **预防建议**：在 GTK 开发中，凡是由主面板/主窗口调起的、需要夺取输入焦点的任何 `transient` 顶级窗口（如 `Gtk.FileChooserDialog`、`Gtk.MessageDialog` 或自定义 `Gtk.Window`），必须在显示前连接其 `show` 与 `destroy` 信号至全局焦点防抖状态机，纳入焦点保护作用域内。

## 三、技术要点沉淀

- **模态窗口/对话框焦点防护模式**：
  ```python
  dialog = Gtk.FileChooserDialog(...)
  dialog.connect("show", lambda *_: self._on_clip_dialog_shown())
  dialog.connect("destroy", lambda *_: self._on_clip_dialog_hidden())
  ```
  - `_on_clip_dialog_shown` 将 `self._dialog_active` 设为 `True`，阻断 `_on_focus_out` 的隐藏执行。
  - `_on_clip_dialog_hidden` 在销毁后通过 `GLib.timeout_add(300, ...)` 延迟将 `_dialog_active` 设为 `False`，避免在焦点返回主窗口的瞬间因过渡失焦再次被折叠。

## 四、后续优化建议

- **统一 Dialog 封装工厂**：可考虑为 `SearchPanel` 下属弹出的所有对话框设计一个统一的辅助工厂方法（如 `create_protected_dialog`），在创建时自动绑定 `show`/`destroy` 焦点防护信号，避免后续开发者手动绑定时出现遗漏。

## 五、参考资料

- [AGENTS.md 中的 GTK 焦点防御惯例](file:///home/hzb/opencode-switcher/AGENTS.md)
- [GtkFileChooserDialog API 官方文档](https://docs.gtk.org/gtk3/class.FileChooserDialog.html)
