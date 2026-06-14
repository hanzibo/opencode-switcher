# 剪切板右键自定义提示词配置功能开发经验总结

> **分支名**：`improve-feature-ask-google-prompt`  
> **开发周期**：2026-06-14 至 2026-06-14  
> **关键词**：`提示词配置` `动态右键菜单` `GTK信号阻断` `即时联动交互`

## 一、经验与教训总结

### 1.1 做得好的地方
- **高拟真与即时交互反馈**：实现了输入框（菜单显示名称）与顶部对应 Tab 标签文字的实时联动更新（通过监听 `changed` 信号），极大增强了操作的高级感与顺畅度。
- **配置数据双向绑定机制**：在切换 Tab、删除当前 Tab 或点击保存时，统一执行当前编辑态的缓存回填（`save_current_active_prompt`），完全避免了用户因忘点保存而丢失配置的可能。
- **自适应滚动设计**：Tab 栏顶部包裹在 `Gtk.ScrolledWindow` 中并启用水平滚动，当用户添加过多提示词时，界面布局仍能优雅自适应，不会被横向撑开。
- **存储与展现逻辑彻底解耦**：将 `custom_prompts.json` 的管理逻辑全封装在 `CustomPromptsStore` 数据层，使 UI 控制器只关注组件渲染。

### 1.2 需要改进的地方
- **GTK 组件实例化繁琐**：因为未使用 XML/Glade 形式设计界面，纯代码组装复杂 Modal 布局的 Python 代码略长。后续开发可以通过提炼通用布局辅助方法（如快速创建 Label+Entry 横向组合）来缩短代码。

## 二、关键问题与解决方案记录

### 问题1：实时改变输入值导致事件回流的死循环/数据冲突问题
- **问题描述**：为了让 Tab 标题随输入框实时联动，监听了 `name_entry` 的 `changed` 事件。但在用户切换 Tab 加载新数据执行 `name_entry.set_text(new_name)` 时，这行代码被动触发了 `changed` 信号，强行进入联动修改回调，导致新 Tab 的标题被错误地写入了前一个 Tab 的旧数据，甚至引起数据错乱。
- **原因分析**：GTK3 的 `Entry.set_text` 等 API 会触发对应的改变信号，属于“代码主动触发事件”，与“用户手动输入触发事件”混在一起导致了信号流污染。
- **解决过程**：
  1. 在连接信号时获取 Handler 唯一标识 ID：`changed_handler_id = name_entry.connect("changed", on_name_changed)`。
  2. 在加载数据（`load_prompt_to_fields`）前，调用 `name_entry.handler_block(changed_handler_id)` 锁定监听。
  3. 执行完 `set_text` 后，调用 `name_entry.handler_unblock(changed_handler_id)` 解除锁定。
- **最终方案**：
  通过临时阻断（Block/Unblock）机制，完美将“代码写入”和“人工输入”的事件流进行分流，避免了逻辑冲突。
- **预防建议**：
  在 GTK 等基于事件驱动的 GUI 开发中，若对某个控件的值进行了双向绑定或单向监听，在程序代码主动更新该控件的值前，**必须先阻断（Block）事件处理器，更新完后再恢复（Unblock）**。

## 三、技术要点沉淀

- **GTK Widget 信号时序控制 API**：
  * `widget.handler_block(handler_id)`：暂时阻断指定 ID 的信号响应。
  * `widget.handler_unblock(handler_id)`：重新开启该信号响应。
- **文字自动折行设定**：
  使用 `Gtk.TextView` 做长文本段落输入时，一定要设置 `set_wrap_mode(Gtk.WrapMode.WORD)` 以适配单词或中文标点换行，否则长句子会直接横向溢出。

## 四、后续优化建议

- **拖拽排序（Drag & Drop）支持**：
  当前提示词配置 Tab 仅能通过新增和删除决定物理顺序。未来如果提示词种类繁多，可支持类似于分类或条目的拖动重新排序，进一步增强交互体验。

## 五、参考资料

- [PyGObject GObject.Object.handler_block API 文档](https://pygobject.readthedocs.io/en/latest/index.html)
