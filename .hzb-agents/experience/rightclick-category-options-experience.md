# 右键类别菜单添加更多选项 开发经验总结

> **分支名**：`add-feature-rightclick-category-options`
> **开发周期**：2026-06-11（约 5 分钟）
> **关键词**：`右键菜单` `类别管理` `GTK3` `代码复用` `MenuShell`

## 一、经验与教训总结

### 1.1 做得好的地方

- **最大程度复用现有代码**：新增的 "Rename" 和 "Delete" 菜单项直接绑定到工具栏按钮的已有方法 `_on_rename_category_clicked` 和 `_on_delete_category_clicked`，无需重复实现重命名/删除逻辑。
- **改动范围最小化**：仅在一个方法（`_on_category_button`）中添加了 11 行代码，单文件修改，零新增依赖。
- **与现有菜单结构一致**：GTK 菜单项创建模式（`Gtk.MenuItem.new_with_label` → `connect("activate", ...)` → `menu.append`）与项目中已有右键菜单完全一致。
- **利用已有焦点保护**：现有 `on_menu_shown`/`_on_menu_deactivated` 回调链自动覆盖新增菜单项，无需额外处理。

### 1.2 需要改进的地方

- **无**：本次改动极为简单，无明显可改进点。

## 二、关键问题与解决方案记录

本次无关键问题。功能直接、一次性通过。

## 三、技术要点沉淀

### 右键菜单扩展模式

在 GTK3 中向现有右键菜单添加选项的标准模式：

```python
menu = Gtk.Menu.new()

# 已有选项
item1 = Gtk.MenuItem.new_with_label("Existing")
menu.append(item1)

# 分隔线
menu.append(Gtk.SeparatorMenuItem.new())

# 新增选项 — 直接复用现有方法
new_item = Gtk.MenuItem.new_with_label("New Option")
new_item.connect("activate", lambda *_: self._existing_method())
menu.append(new_item)

menu.show_all()
menu.popup(...)
```

关键要点：
- `Gtk.SeparatorMenuItem` 用于逻辑分组
- `lambda *_: self._method(None)` 传递 `None` 模拟按钮点击的 `_btn` 参数
- 复用已有方法时需确保 `self._active_category_id` 在调用时已正确设置（右键选择行触发 `_on_category_selected` 会更新它）

## 四、后续优化建议

- 右键菜单中可再添加 "Show at Top/Remove from Top" 和 "Rename/Delete" 之间的分隔线（已实现）
- 未来若增加更多类别操作（如复制类别、导出类别模板），同样追加到此处即可

## 五、参考资料

- [GTK3 GtkMenu / GtkMenuItem](https://docs.gtk.org/gtk3/class.MenuItem.html)
