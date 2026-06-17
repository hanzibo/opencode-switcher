# 剪切板复制内容分类特性 开发经验总结

> **分支名**：`add-feature-classify-copied-content`  
> **开发周期**：2026-06-17 至 2026-06-17  
> **关键词**：`剪切板分类` `GTK-UI过滤` `MIME分析` `Gjs插件` `GTK显示机制`

## 一、经验与教训总结

### 1.1 做得好的地方
- **多端逻辑对齐**：对于复制内容的分类（`text`、`image`、`link`、`code`）以及敏感数据过滤，分别在 GNOME Shell 插件（JavaScript）与后端后台轮询逻辑（Python）中实现了一致的检测规则。
- **敏感数据安全防范**：增加了对密码管理器等软件设置的 `x-kde-passwordManagerHint` MIME 属性的感知，在 Wayland 和 X11 下都能主动放弃敏感数据的录入，提升了软件的安全性。
- **高响应度前端过滤**：利用 GTK 的 `ListBox` 内存过滤机制（`invalidate_filter`）进行联动，保证了在有大量历史项时切换分类依旧能瞬间响应。
- **一键平滑升级**：增加了历史记录的一键自动迁移脚本 `migrate_history.py`，启动时全自动修复旧数据的 `type` 字段，避免了版本升级导致旧数据类型丢失。

### 1.2 需要改进的地方
- **GTK 的递归显示机制认知不足**：在隐藏/显示特定局部组件（如胶囊 Tab 栏）时，忽略了父容器在窗口展示时递归调用 `show_all()` 会强制覆盖子部件隐藏状态（`.hide()`）的问题，导致在非 Clipboard 类别下分类栏偶发显示。
- **GTK set_no_show_all Hens 的细节误区**：在为 Tab 容器设置了 `set_no_show_all(True)` 之后，误以为在容器上直接调用 `show_all()` 可以强行显示其内含子按钮，而实际上 `set_no_show_all(True)` 会使该容器直接屏蔽任何 `show_all` 行为（包含自身调用）。正确做法是显式对子控件在创建时调用 `.show()`，并在父容器上使用 `.show()`。
- **跨类调用私有方法**：最初设计时，在 `clipboard_panel.py` 中直接调用了 `ClipboardStore` 的私有方法 `_classify_text`，破坏了类之间的封装性。

---

## 二、关键问题与解决方案记录

### 问题1：普通类别模板区域偶发显示顶部的胶囊类别Tab
- **问题描述**：在切换到非 "Clipboard" 的普通自定义类别（如“分支管理”）后，顶部本该仅在剪切板下显示的 Tab 分类过滤栏仍然显示了出来。
- **原因分析**：为了展示面板，在 `panel.py` 中调用了 `self._window.show_all()`。GTK 的 `show_all()` 具有向下递归传播的特性，它会强制调用所有子控件（包括我们刚刚通过 `_filter_tabs_box.hide()` 隐藏的 Tab 栏）的 `show()` 方法，导致隐藏状态失效。
- **解决过程**：
  在 [clipboard_panel.py](file:///home/hzb/opencode-switcher/clipboard_panel.py#L169) 初始化 Tab 栏容器时设置 `self._filter_tabs_box.set_no_show_all(True)`，以使其避开父容器的递归 `show_all` 扫描。
- **最终方案**：
  设置了 `set_no_show_all(True)` 后，Tab 栏的显隐完全由我们自己在 `_rebuild()` 中调用 `show()` 与 `hide()` 手动接管。
- **预防建议**：
  对于在同一界面中需要动态显隐的局部容器或面板组件，应在初始化时立即设置 `set_no_show_all(True)`，防止被上层容器的 `show_all()` 调用强制重置。

### 问题2：设置 no-show-all 后，切换回剪切板类别时不显示分类 Tab 按钮
- **问题描述**：修复问题 1 后，切换回 Clipboard 页面时，Tab 栏显示为空白，内部的分类按钮无法正常渲染。
- **原因分析**：
  1. `set_no_show_all(True)` 使得全局 `show_all()` 不再触及该 Tab 栏子树。
  2. 初始化 Tab 按钮时，按钮没有显式调用过 `btn.show()`。
  3. `_rebuild()` 中最初使用的是 `self._filter_tabs_box.show_all()`，但由于 `no-show-all` 属性为 `True`，该方法直接被忽略，子按钮依然保持隐藏状态。
- **解决过程**：
  1. 在 [clipboard_panel.py](file:///home/hzb/opencode-switcher/clipboard_panel.py#L182-L190) 的初始化循环中，对每一个创建的分类按钮显式调用 `btn.show()`，设置它们自身的 visible 状态。
  2. 在 `_rebuild()` 中，将 `show_all()` 还原为 `self._filter_tabs_box.show()`。
- **最终方案**：
  显式将子控件的 visible 设为 `True`，仅通过父容器的 `show()` / `hide()` 来总控整体展示。
- **预防建议**：
  在 GTK 中，一个设置了 `set_no_show_all(True)` 的容器，必须在构造时显式将所有子控件的可见性设为 `True` (`btn.show()`)，切忌对其调用 `show_all()` 来试图点亮子元素。

---

## 三、技术要点沉淀

- **敏感数据过滤 (Sensitive MIME)**：
  在读取系统剪切板时，首先获取剪切板数据的全部 MIME 类型，如果包含特定隐私标记，如 `x-kde-passwordManagerHint`（通常由 KeepassXC、1Password 等密码管理器填充），则必须提前中止复制记录的读取：
  *   **Wayland (wl-paste)**: `wl-paste --list-types`
  *   **X11 (xclip)**: `xclip -selection clipboard -t TARGETS -o`
  *   **GNOME 插件**: `selection.get_mimetypes(...)`
- **可复用的 Python 正则预编译分类器**：
  ```python
  import re
  CODE_KEYWORDS_RE = re.compile(r'\b(const|function|export)\b')
  CURLY_NEWLINE_RE = re.compile(r'[\{\}]\s*[\r\n]|[\r\n]\s*[\{\}]')

  def classify_text(text: str) -> str:
      stripped = text.strip()
      if stripped.startswith("http"):
          return "link"
      if CODE_KEYWORDS_RE.search(text) or CURLY_NEWLINE_RE.search(text):
          return "code"
      return "text"
  ```

---

## 四、后续优化建议

- **分类规则灵活配置**：当前检测 code 和 link 的正则分类逻辑为硬编码，在未来的迭代中，可以将识别代码 and 链接的规则提取至用户配置文件中（如支持自定义正则类型扩展）。
- **非文本类型的分类能力**：目前图片类型是通过标记判断的，未来可以支持直接检测其他 MIME 类型（如 `text/rtf` 或文件路径列表）并引入更多分类标签。

---

## 五、参考资料

- GTK 3 官方文档 - `GtkWidget:no-show-all` 属性说明。
- KeepassXC 官方规范关于敏感数据保护的建议及 `x-kde-passwordManagerHint` 使用方式。
- GNOME Shell Extension `Meta.Selection` APIs。
