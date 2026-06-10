# 搜索与剪贴板面板崩溃修复 开发经验总结

> **分支名**：`fix-feature-panel-unresponsive`  
> **开发周期**：2026-06-10 至 2026-06-10  
> **关键词**：`PyGObject` `SIGSEGV` `Gtk.Dialog` `GtkTooltipWindow` `inline-rename` `signal-disconnect`

## 一、经验与教训总结

### 1.1 做得好的地方
- **高效的调试与定位手段**：面对无任何 Python 异常输出的 C 语言段错误（`SIGSEGV`），引入了 `faulthandler` 模块并配合 `G_DEBUG=fatal-warnings` 在 `gdb` 中启动主程序。这帮助我们在十几秒内定位到了导致崩溃的 C 级别具体 API 级函数调用（`gtk_window_set_transient_for`）。
- **非侵入式测试验证**：在没有真实显示桌面的环境下，编写了头显式的交互测试脚本，利用 `Gtk.main_iteration()` 驱动 GTK 事件循环，成功在沙盒环境中完美复现了两个极其隐蔽的 SEGV 段错误。
- **精确的小部件类型判定**：改变了原先遍历顶层窗口一刀切隐藏销毁的设计，通过限定 `isinstance(win, Gtk.Dialog)` 隔离了 GTK 内部单例小部件，大幅提升了系统的健壮性。

### 1.2 需要改进的地方
- **对 PyGObject 的信号连接机制理解不够深入**：最初使用了 `disconnect_by_func(self.callback)` 来解除信号连接。未料到 Python 的 Bound Method 在每次被访问时均会创建新的方法包装对象，造成 `disconnect_by_func` 悄然失败并在 `try-except` 中被掩盖，导致了后续隐藏面板时重入销毁已释放组件的崩溃。

## 二、关键问题与解决方案记录

### 问题1：取消/关闭新建分类对话框后，再次隐藏并显示面板导致段错误闪退
- **问题描述**：在剪贴板面板中点击“Create”调出新建类别对话框，点击“Cancel”或关闭按钮销毁对话框，随后用快捷键关闭面板并重新打开，主程序瞬间崩溃重启。
- **原因分析**：
  在面板隐藏（`hide()`）时，会遍历 `Gtk.Window.list_toplevels()` 销毁所有隶属于主面板的瞬态窗口。但 GTK 内部存在一个全局隐藏单例 `GtkTooltipWindow`（直接继承自 `Gtk.Window`），当鼠标曾悬停于面板上时，该 tooltip 窗口的 transient parent 也会指向我们的面板。遍历逻辑误将该内部单例也一并隐藏和彻底 `destroy`。下次再重绘面板或需要显示 tooltip 时，GTK 内部尝试为已释放的 Tooltip 窗口设置 transient parent，导致访问了非法悬空指针产生 `SIGSEGV`。
- **解决过程**：
  1. 编写程序化测试脚本，模拟对话框显示、取消、隐藏主窗口、再显示主窗口的过程。
  2. 启用 `G_DEBUG=fatal-warnings` 在 GDB 中运行，截获崩溃现场，堆栈直指 `gtk_window_set_transient_for`。
  3. 打印 `list_toplevels()` 发现包含了 `__gi__.GtkTooltipWindow`。
- **最终方案**：
  在 `panel.py` 的 [hide()](file:///home/hzb/opencode-switcher/panel.py#L419-L425) 方法中增加类型约束，仅销毁 `isinstance(win, Gtk.Dialog)` 的窗口，保留非 `Gtk.Dialog` 继承的全局内部窗口。
- **预防建议**：
  在遍历或清理 GTK 的顶层窗口列表时，千万不可粗暴地对所有 `Gtk.Window` 子类进行 `destroy` 动作，必须基于类型严格白名单过滤，或者建立确切的自定义属性标签。

### 问题2：处于内联重命名输入状态时，按下快捷键隐藏面板导致 100% 崩溃重启
- **问题描述**：在分类侧边栏上点击“Rename”使其进入内联 Entry 编辑状态，此时使用快捷键强制隐藏面板，服务瞬间 SEGV 崩溃重启。
- **原因分析**：
  1. `ClipboardPanel` 原先在 `cancel_rename()` 中使用 `disconnect_by_func(self._on_rename_focus_out)` 取消事件订阅。由于 Bound Method 比较机制，注销操作完全失效。
  2. 隐藏面板触发了主窗口的 `hide()`，进而调用 `cancel_rename()` 重建分类列表，原本处于 Focus 状态的 Entry 容器被清除并销毁。
  3. 在销毁过程中，Entry 触发了 `focus-out-event` 信号，使得未注销的回调被执行。回调再次排队调用 `cancel_rename()`。此时事件机制内部仍在处理该 Entry 的失焦，但外部程序已强行修改小部件层级树，引发冲突性的非法内存写操作崩溃。
- **解决过程**：
  1. 编写内联编辑模拟测试脚本，发现只要带焦点的 Entry 在没有显式解绑并转移焦点的情况下被销毁，100% 触发崩溃。
  2. 捕获到了 `disconnect_by_func` 的 `TypeError` 异常，锁定了未能断开信号连接的问题。
- **最终方案**：
  1. 在 [entry.connect](file:///home/hzb/opencode-switcher/clipboard_panel.py#L1060-L1061) 时保留返回的 Handler ID，在销毁时通过 [disconnect(handler_id)](file:///home/hzb/opencode-switcher/clipboard_panel.py#L997-L1009) 精确解绑信号。
  2. 在重建列表前，主动执行 [toplevel.set_focus(None)](file:///home/hzb/opencode-switcher/clipboard_panel.py#L1011-L1016) 将输入焦点从当前 Entry 移开，实现安全失焦。
- **预防建议**：
  - PyGObject 中必须使用 Handler ID 注销事件订阅以确保绝对可靠。
  - 对于当前承载焦点的输入框，严禁在同步处理逻辑中直接将其从父容器中 `remove` 或 `destroy`，务必提前将焦点移开。

## 三、技术要点沉淀

- **安全断开 PyGObject 信号的模式**：
  ```python
  # 1. 连接时存储 ID
  self._handler_id = widget.connect("event-name", self.callback)
  
  # 2. 注销时通过 ID 精确移除
  if self._handler_id:
      try:
          widget.disconnect(self._handler_id)
      except Exception:
          pass
      self._handler_id = 0
  ```
- **焦点敏感 Widget 安全销毁模式**：
  ```python
  # 转移窗口焦点，避免 widget 带着 focus 被直接销毁引发 GTK 核心段错误
  toplevel = self.get_toplevel()
  if toplevel and hasattr(toplevel, 'set_focus'):
      toplevel.set_focus(None)
  ```

## 四、后续优化建议

- **垃圾回收（GC）管理**：在每次关闭/隐藏主面板后，可以延迟执行一次 `gc.collect()` 以确保所有已解绑的 Python 包装器在 C 对象已被销毁的情况下彻底被清除，避免引用循环造成内存攀升。
- **对输入控件进行统一封装**：如果未来还有其他地方需要使用内联编辑 Entry，建议将其继承或封装为自定义小部件类，并在其自带的析构/隐藏逻辑中妥善处理信号解绑 and 失焦保护。

## 五、参考资料

- [PyGObject 官方 API 接口参考手册](https://pygobject.readthedocs.io/)
- [Gtk.Window 瞬态配置规范说明](https://docs.gtk.org/gtk3/method.Window.set_transient_for.html)
