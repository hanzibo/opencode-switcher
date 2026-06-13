# 自定义类别重排序功能开发经验总结

> **分支名**：`add-feature-category-sort`  
> **开发周期**：2026-06-13 至 2026-06-13  
> **关键词**：`Category Sort` `Gtk.Notebook` `Drag and Drop` `Gtk.ListBox` `Transactional Save` `Focus Guards`

## 一、经验与教训总结

### 1.1 做得好的地方
- **拖拽行为的高度模块化封装**：置顶与非置顶类别重排虽然分属不同的标签页和 `ListBox` 实例，但其底层的拖拽 Motion、Data Received 以及 Viewport Clamping 算法完全一致。本方案提炼出了 `setup_dnd(listbox, scrolled, items, rebuild_func)` 闭包工厂函数，避免了大量冗余的代码拷贝，极大地提升了逻辑内聚度。
- **简捷的数据主顺序映射设计**：抛弃了原先每次 `get_all()` 时基于 `created_at` 的硬编码逆序排序逻辑，将 `CategoryStore._categories` 列表中的固有元素顺序作为真实的物理排序。新建分类默认以 `insert(0, ...)` 方式插入头部以兼容旧有习惯。这使得排序落盘操作变得异常直观。
- **校验健全的数据层防线**：在 `reorder_categories` 持久化写入前增加了 ID 集合比对校验（`orig_ids != new_ids`），能够完全杜绝由于 UI 渲染副本层发生未知崩塌时向磁盘写入损毁数据的风险。

### 1.2 需要改进的地方
- **对系统预设类别的边界剥离**：`Clipboard` 类别在逻辑上为置顶状态，但绝不应参与用户的任何重排序。开发初期必须确立 `c.id != "__clipboard__"` 的过滤策略，避免将系统预设项混入排序对话框。

## 二、关键问题与解决方案记录

### 问题1：如何优雅实现多标签页内独立拖拽排序而互不干扰
- **问题描述**：在同一个 Notebook 弹窗下有两个分别用来承载“置顶”和“普通”分类的 ListBox，如何确保它们能够各自独立地进行重排，且在拖拽动作发生时能够定位准确且不相互污染。
- **原因分析**：
  1. 拖放（DnD）事件中的鼠标绝对坐标相对于全局/对话框视口，必须用当前所属的 `ScrolledWindow` 视口 Vertical Adjustment 重新修正（Clamping）；
  2. 若两套列表共享变量指针，容易造成上下文变量在 Tab 切换或同时 hover 时混乱。
- **解决过程**：
  - 构建闭包工厂函数 `setup_dnd(lb, scr, items, rebuild_func)`，在其中通过 `nonlocal` 来捕获和维持各自列表的 `_current_hover_row` 和 `_current_hover_dir` 局部状态；
  - 传入各自的滚动调校接口和数据引用数组，使它们互不重合。
- **最终方案**：利用 Python 闭包封装 Gtk 拖拽监听，使得置顶与普通类别在各自列表重构时均能完全独立运行。
- **预防建议**：当一个弹窗内同时出现多个可拖拽列表时，应当使用函数工厂或类封装等方式隔离各自的事件监听环境与局部状态机。

## 三、技术要点沉淀

- **可复用的 ListBox 双重拖拽闭包工厂模式**：
  ```python
  def setup_dnd(lb, scr, items, rebuild_func):
      _current_hover_row = None
      _current_hover_dir = None
      # on_drag_motion / on_drag_leave / on_drag_data_received
      ...
      lb.connect("drag-motion", on_drag_motion)
      lb.connect("drag-data-received", on_drag_data_received)
      return on_drag_data_get, on_drag_end
  ```
- **双 Tab 页 Gtk.Notebook 结构参考模板**：
  ```python
  notebook = Gtk.Notebook.new()
  page1 = Gtk.Box.new(Gtk.Orientation.VERTICAL, 8)
  # Pack scrollable listbox into page1...
  notebook.append_page(page1, Gtk.Label.new("Pinned Categories"))
  ```

## 四、后续优化建议

- **拖拽过程视觉特效增强**：当前拖动时只显示指示线（蓝色 Border），未来可以加入被拖拽行项目的半透明缩略图（GdkPixbuf）随鼠标滑动的交互细节，使用户体验更上一层楼。

## 五、参考资料

- [GTK+ 3 Python - GtkNotebook Examples](https://pygobject.readthedocs.io/en/latest/guide/gtk/widgets.html)
