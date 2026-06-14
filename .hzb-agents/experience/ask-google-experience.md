# 右键菜单新增 Ask Google 快捷搜索功能开发经验总结

> **分支名**：`add-feature-ask-google`  
> **开发周期**：2026-06-14 至 2026-06-14  
> **关键词**：`右键菜单` `Google搜索` `文本截断` `窗口聚焦` `代码重构`

## 一、经验与教训总结

### 1.1 做得好的地方
- **UI 规范一致性**：在 `Copy` 和 `Delete` 菜单项下方新增了水平分割线（`Gtk.SeparatorMenuItem`），使得操作区域从视觉上能与外部扩展区清晰分离。
- **边界条件防御设计**：针对历史项是否为文本进行了类型过滤（`getattr(item, "type", "text") == "text"`），使得图片或文件等非文本类型被置灰，有效防止了后续报错。
- **重构消冗余**：敏锐地发现了原先定义在 `panel.py` 内部的 GNOME 窗口聚焦函数（`_request_window_focus`）在 `clipboard_panel.py` 中被复制使用，及时将其重构并提炼到了 `utils.py` 中，使代码更加清爽可维护。
- **进程启动鲁棒性**：通过 `subprocess.Popen` 直接调用 `firefox` 时，将输出重定向至 `/dev/null` 避免终端垃圾信息，且增加了 `Gtk.show_uri_on_window` 进行通用浏览器兜底。

### 1.2 需要改进的地方
- **初版类型批注缺失**：在编写 `_ask_google` 时，形参 `item` 一开始未声明类型限制，静态提示不够完善。后续在优化审查阶段加上了 `item: ClipboardItem` 的显式声明。

## 二、关键问题与解决方案记录

### 问题1：大段文本搜索导致浏览器 414 URI Too Long 错误
- **问题描述**：当选中的剪切板内容过长（比如数万字的代码），直接通过 URL GET 请求参数传递会导致 Firefox 报错，无法正常搜索。
- **原因分析**：各大浏览器及 Google 对 GET URL 长度有默认限制（一般建议在 2000 字节以内）。
- **解决过程**：
  1. 计算固定追加的 Suffix 长度（`" 以上内容是什么意思，如果是代码，请分析并注释。 "` 为 31 字符）。
  2. 当 `len(final_query) > 2000` 时，动态截断 original 文本为 `2000 - 31 = 1969`。
  3. 通过 `Gtk.MessageDialog` 向用户展示截断警告。
- **最终方案**：
  在拼接前检查字数。若超长则弹出模态对话框警告，用户点击 OK 后，以截断至安全长度的文本执行 Google Search。
- **预防建议**：
  任何涉及 URL 传参的操作，都需要做最大长度限制与溢出防御，不能直接信任外部数据长度。

### 问题2：窗口焦点锁定状态机（Focus Guard）防抖处理
- **问题描述**：在右键弹出 MessageDialog 对话框后，关闭对话框可能会意外触发主面板的 `focus-out-event` 从而使得整个 OpenCode Switcher 界面隐藏。
- **原因分析**：GTK 主窗口在显示二级对话框时，焦点会转移。如果没有正确的焦点防护逻辑，对话框销毁时主窗口可能会检测到失去焦点从而直接收起。
- **解决过程**：
  通过在弹出警告对话框时，调用 `self.on_dialog_shown()` 设置全局 `_dialog_active = True`；并在对话框销毁的 response 回调中调用 `self.on_dialog_hidden()` 重置该值。
- **最终方案**：
  严格沿用项目内置的 focus protector 信号绑定模式，确保对话框生命周期与主界面显示防抖步调一致。
- **预防建议**：
  在使用 PyGObject/GTK3 时，凡是由主面板拉起的模态/非模态对话框，必须注册 `transient_for` 并在显示和销毁阶段正确管理父窗口的 `_dialog_active` 状态。

## 三、技术要点沉淀

- **水平分割线使用**：
  ```python
  sep = Gtk.SeparatorMenuItem.new()
  menu.append(sep)
  ```
- **GNOME Wayland/X11 窗口聚焦扩展交互**：
  通过将 `firefox` 写入 `~/.cache/opencode-switcher/focus.request`，GNOME Shell 扩展会读取该文件并调用相关 API 强行将 Firefox 唤起到最前台，绕过了 Wayland 无法获取系统级激活焦点的技术屏障。
- **后台进程无干扰运行模式**：
  ```python
  subprocess.Popen(["firefox", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
  ```

## 四、后续优化建议

- **自定义浏览器配置**：
  目前采用硬编码 `firefox` 或系统默认浏览器。若用户习惯使用 `chrome` 等其他浏览器，可在后期引入全局配置文件，允许用户自定义全局搜索使用的浏览器命令。

## 五、参考资料

- [Gtk.MessageDialog - PyGObject API 文档](https://pygobject.readthedocs.io/en/latest/index.html)
- [RFC 2616 - Hypertext Transfer Protocol (URL Length constraints)](https://datatracker.ietf.org/doc/html/rfc2616)
