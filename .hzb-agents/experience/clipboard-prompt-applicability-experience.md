# 提示词类别适用性扩展 开发经验总结

> **分支名**：`improve-feature-clipboard-google-search`  
> **开发周期**：2026-06-17 至 2026-06-17  
> **关键词**：`提示词配置` `右键菜单过滤` `全选联动` `类型安全` `向下兼容`

## 一、经验与教训总结

### 1.1 做得好的地方
- **优雅的向下兼容**：利用 Python 字典解析，在数据加载阶段无缝地为旧配置注入默认的 `["text"]`（文本）属性，无须编写额外的数据库迁移文件，保证了系统在升级后能零成本兼容历史配置。
- **级联死循环规避**：在实现“全选”与“单个类别”复选框的双向选择状态同步时，使用 `updating_checks = [False]` 状态锁，完美阻断了 GUI 事件循环风暴，保证了界面运行的高响应度与高可靠性。
- **极简精当的重构**：及时把两处提取 Checkbox 状态的代码提炼为内部公用辅助函数 `get_selected_categories()`，符合 DRY（Don't Repeat Yourself）原则，降低了后期维护的复杂度。

### 1.2 需要改进的地方
- **首版编写时的重构意识**：最初提交的代码中直接在 `save_current_active_prompt` 和 `on_confirm_clicked` 复制粘贴了复选框读取逻辑，在后续的审查与优化阶段才提炼出了辅助函数。未来开发中应更敏锐地在第一次编写时便识别并消除重复代码。
- **类型提示的严谨度**：最初在 `CustomPrompt` 扩展 dataclass 属性时漏掉了 `Optional` 的显式类型注解（误标为了 `List[str] = None`），虽然运行时通过，但应该一开始就保持类型系统的高严谨性。

---

## 二、关键问题与解决方案记录

### 问题1：级联触发的无限循环 UI 事件风暴
- **问题描述**：勾选“全选”会更新子项的选择状态，而子项的状态改变反过来会重算并触发“全选”状态的改变，在未设防的初始状态下可能导致循环嵌套调用，引发界面崩溃或僵死。
- **原因分析**：这是典型的 GUI 父子联动信号自反馈循环。
- **解决过程**：
  在 `_show_prompts_config_dialog` 内部声明一个可改写的标志位 `updating_checks = [False]`。当进入任何一个状态变更函数时，如果发现该标志已为 `True`，直接提前 `return` 中断，否则设为 `True` 执行状态写入并在末尾还原。
- **最终方案**：
  使用一处标记锁对联动事件进行了全面保护。
- **预防建议**：
  在设计任何双向联动或多级联动控件（如三态树、级联下拉列表、全选复选框等）时，事件回调中必须使用状态标记锁防范。

### 问题2：未勾选任何类别时数据被保存导致提示词失联
- **问题描述**：用户如果不小心取消勾选了所有类别（文本、链接、代码）并点击了 Confirm，会导致该提示词配置无法在任何右键上下文菜单中呈现，形同失联。
- **原因分析**：输入保存时缺乏合理性边界校验。
- **解决过程**：
  在 `on_confirm_clicked` 中加入了有效性阻断校验。若勾选为空列表，则生成一个非阻塞的 `Gtk.MessageDialog` 警告框提醒用户，并直接 `return` 阻止程序保存与销毁配置弹窗。同时在保存循环中为所有提示词的 categories 做降级保护。
- **最终方案**：
  增加了空勾选校验与 MessageDialog 拦截。
- **预防建议**：
  凡是支持用户自由删除/取消的配置项目，在 Confirm 保存前必须对边界值（非空、长度、空值）执行一致的强制检验。

---

## 三、技术要点沉淀

- **全选与单选框的联动控制模板**：
  ```python
  updating_checks = [False]

  def update_select_all_state():
      if updating_checks[0]:
          return
      updating_checks[0] = True
      all_checked = check1.get_active() and check2.get_active() and check3.get_active()
      select_all_check.set_active(all_checked)
      updating_checks[0] = False

  def on_select_all_toggled(widget):
      if updating_checks[0]:
          return
      updating_checks[0] = True
      active = widget.get_active()
      check1.set_active(active)
      check2.set_active(active)
      check3.set_active(active)
      updating_checks[0] = False
  ```
- **右键上下文菜单中的按类别精确过滤**：
  利用 Python 内建的 `getattr(item, "type", "text")` 获取剪切板项目被打标的类别，并直接利用 `item_type in p.categories` 完成渲染期过滤，比老版的置灰模式（`set_sensitive`）更具极简感。

---

## 四、后续优化建议

- **图片支持文本拼接的场景扩展**：目前图片属于绝对排他，未来如果引入 OCR（光学字符识别），可以通过触发 OCR 将图片中的文字提取后再使用自定义提示词传递给 Google 搜索，从而使自定义菜单对图片也生效。

---

## 五、参考资料

- GTK 官方关于 `Gtk.CheckButton` 信号处理的最佳实践。
- PyGObject (GObject Introspection) 异步对话框处理。
