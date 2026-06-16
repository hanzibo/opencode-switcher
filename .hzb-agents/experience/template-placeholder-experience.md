# 智能占位提示符与默认值语法扩展 开发经验总结

> **分支名**：`improve-feature-template-placeholder-autofill`  
> **开发周期**：2026-06-16 至 2026-06-16  
> **关键词**：`动态模板` `占位符` `默认值` `Cairo绘制` `GtkTextView` `PyGObject`

## 一、经验与教训总结

### 1.1 做得好的地方
- **多语法机制优雅融合**：在单套正则表达式下兼容了传统的 `${1}`、占位提示 `${1:提示}` 和默认值 `${2=默认}` 三种语法，实现了解耦与向下兼容。
- **Cairo 高阶自绘实践**：克服了 GTK3 `Gtk.TextView` 原生不支持占位符的局限，成功使用 Cairo 与 Pango 引擎实现了视觉效果极佳的动态占位提示。
- **边界防干扰隔离**：在直接复制逻辑（`_activate_item`）的过滤中，通过对象类型区分（仅限 `CategoryItem`），有效避免了对用户剪切板历史中普通 Linux Shell 命令（含有类似 `${1:-default}`）的修改与破坏。
- **代码重构与整洁性**：根据代码质量审查建议，将正则表达式硬编码预编译为类常量集中管理，并对依赖包导入位置进行了规范。

### 1.2 需要改进的地方
- **对 GdkWindow 绘图层叠机制理解不深**：最初使用 `connect` 连接 `draw` 信号，忽视了 GTK 组件绘制背景时对自定义像素的覆盖。应该第一时间采用 `connect_after` 和专用 `TextWindow` 过滤。
- **对布局样式的计算不够直接**：前期依赖手动累加 `left_margin`，没有充分利用官方 API 进行 CSS Padding 等主题样式的融合，导致提示词渲染与光标产生了数像素的偏斜。

## 二、关键问题与解决方案记录

### 问题1：Gtk.TextView 自定义占位词被默认背景层覆盖抹去
- **问题描述**：Dynamic Copy 对话框打开时，输入区域为空，但未显示出任何灰色的占位文字。
- **原因分析**：
  1. 信号连接使用了普通的 `connect`，其运行在 GTK 默认绘制（擦除并渲染组件背景）之前，导致绘制的文字被背景擦除覆盖。
  2. 未限定在 `Gtk.TextWindowType.TEXT` 专有窗口中，导致其随整个组件多次重绘而被遮挡。
- **解决过程**：使用 `.connect_after()` 方法连接信号，并在 `on_textview_draw` 中使用 `Gtk.cairo_should_draw_window` 校验只在实际文本编辑窗口发生时才开始自绘。
- **最终方案**：
  ```python
  tv_in.connect_after("draw", on_textview_draw)
  ```
  在 draw 回调中：
  ```python
  text_window = widget.get_window(Gtk.TextWindowType.TEXT)
  if text_window and Gtk.cairo_should_draw_window(cr, text_window):
      # 执行 Cairo 渲染...
  ```
- **预防建议**：在 PyGObject GTK3 中对复合组件的子区域（如 TextView、ScrolledWindow 等）执行图形叠加时，必须显式调用 `cairo_should_draw_window` 限制当前 GdkWindow 范围。

### 问题2：占位词渲染起点与文本框输入光标偏离不对齐
- **问题描述**：提示文字偏左，光标在第一个字的中间闪烁，没有整齐左对齐。
- **原因分析**：只使用了 `get_left_margin()` 返回的 `8px`。而系统主题 CSS 中的 `padding` 也会调整文本真正的绘制起点。实际起点为 `Margin + CSS Padding`，导致只按 Margin 翻译的提示语偏左。
- **解决过程**：通过获取首字符的逻辑 iter 指针，换算出真实的逻辑坐标，再将其通过坐标映射转换为绝对窗口坐标。
- **最终方案**：
  ```python
  start_iter = buf.get_start_iter()
  rect = widget.get_iter_location(start_iter)
  left, top = widget.buffer_to_window_coords(Gtk.TextWindowType.TEXT, rect.x, rect.y)
  cr.translate(left, top)
  ```
- **预防建议**：在文本编辑器中做文字绘制或高亮，应避免依赖手动相加 margin，直接使用 `get_iter_location` 结合 `buffer_to_window_coords` 可以自动且准确地融合主题 CSS 样式。

### 问题3：直接复制/激活模板时默认值未完成替换与提示符暴露
- **问题描述**：用户右键直接点击“Copy”或在列表双击模板，复制出的内容保留了原始的 `${1=李三}` 或 `${1:用户名}` 格式。
- **原因分析**：`_activate_item` 原先直接将 `item.text` 原样复制，只在 Dynamic Copy 对话框提交时重写了文本。
- **解决过程**：将模板规范化与替换逻辑重构提取为 `_process_template_text(text)` 辅助方法，并在 `_activate_item` 的 `CategoryItem` 判定分支中提前执行预处理。
- **最终方案**：
  ```python
  def _process_template_text(self, text: str) -> str:
      def repl(match):
          num = match.group(1)
          op = match.group(2)
          val = match.group(3)
          return val if op == "=" else f"${{{num}}}"
      return TEMPLATE_REGEX.sub(repl, text)
  ```
- **预防建议**：模板解析与数据转换应该在 UI 输出的入口和复制的最终出口实现闭环保护。

## 三、技术要点沉淀

- **Cairo + Pango 跨图层文本绘制**：适用于不支持原生 placeholder 的所有复杂文本组件自绘，在 GTK3 开发中是非常实用的补齐技巧。
- **TextView 全选（Select All）模式**：
  ```python
  def on_focus_in(widget, event):
      buf = widget.get_buffer()
      start, end = buf.get_bounds()
      buf.select_range(start, end)
      return False
  tv.connect("focus-in-event", on_focus_in)
  ```
- **Regex 反向组引用 lambda 代换**：使用 `re.sub(pattern, lambda m: val, text)`，相比直接传入字符串，能完美避免 `val` 中自带反斜杠（如 Windows 路径中的 `\U`、`\t`）引起的组解意错误。

## 四、后续优化建议

- **样式差异化配置**：为默认值设置特定的高亮颜色，提示用户此部分数据为自动填充。
- **多行默认值高度适配**：目前默认值仅为单行替换。如果默认值包含多行文本，可在输入框加载后自动增高输入区行数。

## 五、参考资料

- [PyGObject Gtk.TextView API Docs](https://pygobject.readthedocs.io/en/latest/bindings/gtk/gtk_text_view.html)
- [PangoCairo Layout Rendering Guide](https://pygobject.readthedocs.io/en/latest/bindings/pango/pango.html)
- [Gtk3 CSS Properties Manual](https://docs.gtk.org/gtk3/css-properties.html)
