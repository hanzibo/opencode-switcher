# 智能动态模板 (Dynamic Copy) 开发经验总结

> **分支名**：`add-feature-dynamic-copy`  
> **开发周期**：2026-06-13 至 2026-06-13  
> **关键词**：`动态复制` `双栏布局` `键盘焦点导航` `实时数据绑定` `Gtk.Notebook` `Gtk.TextView`

## 一、经验与教训总结

### 1.1 做得好的地方
- **状态防御与去重排序**：利用 `set` 和 `sorted` 实现了占位符数字的高效提取、去重以及升序标签页排列。并且通过 `is_updating_preview` 状态哨兵和 `try-finally` 结构，完美避免了输入改变时频繁重载渲染造成的 UI 死循环卡死。
- **卓越的键盘流交互（UX）**：在输入框内成功拦截 Tab 和 Shift+Tab，不仅实现了标签页的前后快速切换，并在最后一个标签页按 Tab 时，将焦点精准移动至 `Copy` 按钮。同时在任何文本框支持 `Ctrl+Enter` 快速复制提交，实现了极佳的键盘操作流体验。
- **一致性布局适配**：在列表项右侧动态增加绿色字体标识 `Dynamic Copy`，并在 Light 主题下彻底清除了 `Gtk.Notebook` 默认的黑色边框，保持了整体视觉的高度契合与一致性。

### 1.2 需要改进的地方
- **右键菜单语法结构被误写**：在首次插入 `Dynamic Copy` 菜单项时，因对 `_on_content_button` 的 python 代码缩进和 `if-else` 分支语法确认不仔细，导致 `else:` 分支被误删，引发逻辑结构混乱。后续在大块替换时，必须严格比对和校验 Python 的缩进结构。

## 二、关键问题与解决方案记录

### 问题1：Light（亮色）主题下，Dynamic Copy 左侧输入框外层存在突兀的黑色边框
- **问题描述**：在白色/亮色主题下打开 `Dynamic Copy` 或 `Sort Categories` 弹窗时，左侧的参数输入标签页下方会渲染出一圈非常明显的黑色框体，严重影响美观。
- **原因分析**：GTK3 原生 `Gtk.Notebook` 的内容容器（`stack`）在一些特定的桌面主题中默认会绘制厚重的框线（Border Frame）。
- **解决过程**：
  1. 在 `Gtk.Notebook` 实例化后，显式调用原生 API 关闭边框：`notebook.set_show_border(False)`。
  2. 在 `clipboard_panel.py` 的 CSS 样式块中，对 `.custom-dialog notebook` 及其 `stack` 节点强行覆盖 `border: none; background-color: transparent;` 属性。
- **最终方案**：通过 API 设置与 CSS 覆盖的双重方式彻底清空框体渲染。
- **预防建议**：在 GTK3 中凡是用到 `Gtk.Notebook` 且需要融入自定义背景的窗口，都应当在创建时显式执行 `set_show_border(False)`，并在 CSS 中覆盖 `notebook > stack` 的 `border: none`。

### 问题2：占位符变量双向编辑冲突
- **问题描述**：右栏预览框作为二次微调编辑框时，如果用户手动修改了预览文本，但在左栏 Tab 键入新内容时，预览框直接编辑的部分会被覆盖。
- **原因分析**：左栏输入框的 `changed` 信号触发了基于原模板 `item.text` 的全量重渲染，这势必会覆盖对预览框的临时编辑。
- **解决过程**：明确交互数据流顺序：左栏输入 $\rightarrow$ 驱动右栏模板预览 $\rightarrow$ 预览文本拼装完成后在右栏进行最终微调 $\rightarrow$ 点击 Copy。此属于合理的数据单向流动，符合期望。
- **预防建议**：双向编辑时需确保变量输入框的变动不会被误覆盖，本方案选择让变量驱动只覆盖当前全量组装，而在预览文本微调期间不改变左栏变量，实现清晰的“最终微调”逻辑语义。

## 三、技术要点沉淀

- **Gtk.TextView 内边距优化模式**：
  ```python
  tv.set_left_margin(8)
  tv.set_right_margin(8)
  tv.set_top_margin(8)
  tv.set_bottom_margin(8)
  ```
- **双向 Tab 键 & 快捷键劫持机制**：
  ```python
  def on_key_press(widget, event, num_val):
      is_shift = (event.state & Gdk.ModifierType.SHIFT_MASK) != 0
      # Shift+Tab
      if event.keyval == Gdk.KEY_ISO_Left_Tab or (event.keyval == Gdk.KEY_Tab and is_shift):
          # 切回上一个 Page
          return True
      # Tab
      if event.keyval == Gdk.KEY_Tab:
          # 切到下一个 Page 或聚焦 Copy 按钮
          return True
      # Ctrl/Cmd+Enter
      is_enter = event.keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter)
      has_modifier = (event.state & (Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.MOD4_MASK | Gdk.ModifierType.META_MASK)) != 0
      if is_enter and has_modifier:
          on_confirm(None)
          return True
      return False
  ```

## 四、后续优化建议

- **占位符正则多格式兼容**：当前系统正则 `\$\{(\d+)\}` 仅支持纯数字占位符。未来如果模板拓展，可将其增强为支持命名变量（如 `${username}`），自动将首个字符大写作为 Tab 标签名称，从而增强模板泛用性。

## 五、参考资料

- [AGENTS.md 中的 GTK 线程安全与焦点防御](file:///home/hzb/opencode-switcher/AGENTS.md)
- [GtkNotebook API 官方文档](https://docs.gtk.org/gtk3/class.Notebook.html)
