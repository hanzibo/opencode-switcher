# 模板项目拖拽重排 (Template Item DnD Sort) 开发经验总结

> **分支名**：`add-feature-content-template-sort`  
> **开发周期**：2026-06-11 至 2026-06-12  
> **关键词**：`GTK3` `拖放 (DnD)` `列表框 (ListBox)` `坐标越界限幅` `内存泄漏` `布局失效 (Layout Invalidation)`

## 一、经验与教训总结

### 1.1 做得好的地方
- **从动态 DOM 节点改动演进至静态 CSS 类修饰**：最初采用在行之间动态增删物理分隔行 Widget 的方式展示指示线，导致极高频的 `queue_resize()` 触发和异步脏布局，定位极不准确。优化后采用全静态高度的行（加 3px 透明 border），Hover 时仅改变其颜色（`.sort-row.drag-hover-top`），完全避免了物理尺寸重组，让坐标识别变得极度稳定。
- **本地独立测试原型快速闭环**：通过编写 `dnd_test.py` 成功解耦了主程序的 UI 面板干扰，得以快速在复杂滚动状态下收集 GDK 拖拽事件坐标数据并快速迭代验证算法。
- **高阶闭包状态变量现代重构**：为主程序和测试脚本清理了 `[None]` 这类 Python2 式列表闭包指针变量包装，采用现代 Python3 的 `nonlocal` 声明管理闭包内部状态，使得语义更为直观、利于阅读。

### 1.2 需要改进的地方
- **GTK CSS 样式的继承域问题识别滞后**：在将 CSS Style Provider 从全局屏幕级（Screen-level）优化到组件级时，误以为在 `ListBox` 上注册即可自动为各子组件行 `ListBoxRow` 继承。导致分割线短暂消失。最终查明 GTK3 不具备非级联 style providers 的自动向下传递性，改在 `build_rows` 的循环中直接为每行 row 各自注册 Provider 解决。

## 二、关键问题与解决方案记录

### 问题1：向上/向下拖拽过快或拖出视口时，元素被意外转移至最底部或偏移 2-3 个位置
- **问题描述**：拖拽过程中若将元素移动速度过快，或者移出当前可见视口上方释放，该元素有较大几率掉落至底部，或者落在比鼠标实际松开位置偏上 2-3 个位置。
- **原因分析**：
  1. **脏布局时差**：动态增删分隔线 Widget 频繁发出 `queue_resize()`，在 GTK 进行异步布局重算时，鼠标松开调用的 `get_row_at_y(y)` 因计算数据陈旧直接返回了 `None`，退回默认的尾部位置（转移至底部）。
  2. **视口越界无阻尼**：在滚动状态下，滚出可见区域的上半部分依然能被 `get_row_at_y` 检测出，导致误认为落点在那些被遮蔽的行中（偏上 2-3 个位置）。
- **解决过程**：
  1. 将 Widget 增删替换为纯 CSS 边框高亮，维护静态高度。
  2. 在 `on_drag_data_received` 和 `on_drag_motion` 中从 ScrolledWindow 取出 vertical adjustment，对拖放坐标 `y` 强行限制到当前可见视图的物理顶部 `visible_top` 和底部 `visible_top + visible_height` 之间。
- **最终方案**：使用 Viewport Clamping 限幅逻辑对鼠标落点坐标进行约束，并利用 `get_row_at_y(clamped_y)` 稳健解析行位置。
- **预防建议**：开发任何滚动视图内的坐标拾取功能时，必须先拉取其 adjustment 对传入坐标做视口约束（Clamping），不能直接信任原生物理拖拽传入的 `y`。

### 问题2：全局 Screen CSS Provider 造成隐式内存泄漏
- **问题描述**：原设计在弹窗展现时利用 `add_provider_for_screen` 添加蓝色指示线样式，但弹窗关闭销毁时并未卸载样式提供者。随着用户反复开启排序弹窗，全局屏幕中堆积了大量冗余的样式提供者。
- **原因分析**：Screen-level 样式提供者与整个 X11/Wayland 屏幕生命周期对齐，除非手动注销，否则在应用生命周期内常驻不退，这在弹窗高频使用时是不合理的。
- **解决过程**：舍弃 Screen-level 注册，将 CSS Provider 专门注册到 `listbox` 及 `ListBoxRow` 实例的 StyleContext 层次中。
- **最终方案**：
  在 `build_rows()` 创建行时，直接对每行 row 调用 `row.get_style_context().add_provider(css, Gtk.STYLE_PROVIDER_PRIORITY_USER)`。
- **预防建议**：除非是影响全局应用级别的主题 CSS 调整，否则局部对话框的样式规则均应注册到对应的 Widget 实例中，使其自动跟随 Widget 销毁而被垃圾回收。

## 三、技术要点沉淀

- **可复用的 GTK3 静态高度拖拽高亮 CSS 模式**：
  ```python
  css = Gtk.CssProvider()
  css.load_from_data(b"""
      .sort-row {
          border-top: 3px solid transparent;
          border-bottom: 3px solid transparent;
      }
      .sort-row.drag-hover-top {
          border-top-color: #3584e4;
      }
      .sort-row.drag-hover-bottom {
          border-bottom-color: #3584e4;
      }
  """)
  ```
  在创建行元素后，将上面的 `css` 赋给行的 style context，在拖放中根据鼠标所在行的物理中点偏移调用 `add_class("drag-hover-top")` / `remove_class()` 即可展现平滑不抖动的指示线。

- **滚动视口 Clamping 坐标限幅模式**：
  ```python
  vadj = scrolled.get_vadjustment()
  if vadj:
      visible_top = vadj.get_value()
      visible_height = vadj.get_page_size()
      clamped_y = max(visible_top, min(y, visible_top + visible_height))
  else:
      clamped_y = y
  ```

## 四、后续优化建议

- **滚动区自动感应阻尼加速**：目前在拖到最上方或最下方时能够实现触发滚动，但滚动速度是固定的。未来可以引入距离视口边缘的差值作为权重，距离越近滚动速度越快，使长列表拖拽交互更为丝滑。

## 五、参考资料

- [GTK+ 3 Reference Manual - GtkStyleContext](https://docs.gtk.org/gtk3/class.StyleContext.html)
- [GdkDragContext Documentation](https://docs.gtk.org/gdk3/class.DragContext.html)
