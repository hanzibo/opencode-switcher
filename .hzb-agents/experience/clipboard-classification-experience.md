# 剪切板内容识别与语言显示优化开发经验总结

> **分支名**：`improve-feature-clipboard-classification`  
> **开发周期**：2026-06-17 至 2026-06-17  
> **关键词**：`剪切板分类` `启发式打分` `语言标识` `GTK3-CSS` `代码重构`

## 一、经验与教训总结

### 1.1 做得好的地方
- **高精度启发式分类设计**：引入了基于绝对前缀匹配（JSON/HTML/Shebang/CLI）与多维特征打分器（Python/CPP/Java/JS/SQL/Shell等）的混合分类算法，大幅提升了代码识别率，同时降低了误判率。
- **模块化代码与高保真对齐**：通过在 `clipboard_store.py` 中将核心判定和探测算法重构为模块级函数，使 `migrate_history.py` 能够通过 `import` 复用逻辑，消除了 200 余行重复代码，确保运行期与迁移期判定逻辑 100% 同源。
- **UI 响应式显示与自适应渲染**：在剪切板列表中为已识别的代码内容右侧、时间标签上方，使用显眼颜色高亮标出具体的编程语言，并在 UI 侧进行了上大写转换适配。

### 1.2 需要改进的地方
- **对 GTK/PyGObject 特性的兼容性估计不足**：在实现 UI 样式阶段，由于直接在 CSS 中使用了主流浏览器支持的 `text-transform: uppercase;`，导致 GTK 3 CSS 解析失败抛出致命异常崩溃。应牢记 GTK 3 样式引擎非常局限，无法支持所有标准 CSS 属性，需要尽可能在 Python 数据端完成此类格式转化。
- **对重构阶段的回顾校验不够充分**：重构早期在 `clipboard_panel.py` 中对语言识别标志的调用中出现拼写错误（错误地把本类的 `self.clipboard_store` 拼写为了 `self._clip_store`），导致特定文本复制时发生运行时属性异常。在以后提交代码前，必须保证所有交互链路（如模拟复制事件）都经历过真实测试覆盖。

## 二、关键问题与解决方案记录

### 问题1：GTK 3 CSS 不支持 `text-transform` 属性导致服务启动崩溃
- **问题描述**：在为代码语言标签定义 CSS 样式时，添加了 `text-transform: uppercase;` 以显示大写的语言名称（如 `PYTHON`, `JAVASCRIPT`），但在 `./install.sh install` 重新运行服务后，服务直接报错未运行，托盘与面板全部不可用。
- **原因分析**：GTK 3 样式引擎对 CSS 标准的支持非常有限，完全不支持 `text-transform` 属性。在此处使用该属性导致 `Gtk.CssProvider` 加载失败抛出致命异常，导致主程序启动崩溃。
- **解决过程**：
  1. 移除 CSS 样式中的 `text-transform: uppercase;`。
  2. 更改为在 Python UI 渲染端进行大写化转换：在 `clipboard_panel.py` 的数据填充处对检测到的语言字符串调用 `.upper()`，再传入 `label.set_text()`。
- **最终方案**：
  在 CSS 中仅保留基础的颜色与大小样式，在 Python 代码层将语言名称转为大写后再展示。
- **预防建议**：
  在开发 GTK3/PyGObject 应用时，严禁使用任何高级或非主流的 CSS 属性（如 `!important`, `text-transform`, `box-shadow` 的复杂多重阴影等）。如果需要转换文本，应优先在 Python 逻辑层完成。

### 问题2：引入语言标识后发生拼写错误（AttributeError）导致 JS 复制代码崩溃
- **问题描述**：在复制一段 JavaScript 代码时，系统托盘图标运行但快捷键无法弹出面板，服务后台日志报错 `AttributeError: 'ClipboardPanel' object has no attribute '_clip_store'`。
- **原因分析**：在 `clipboard_panel.py` 第 543 行左右的 `on_clipboard_owner_change` / 数据添加事件中，为了获取语言信息以决定是否显示，调用了 `self._clip_store.classify_text(item_text)`。然而在 `ClipboardPanel` 类中，剪切板存储库的实例变量名应为 `self.clipboard_store`。
- **解决过程**：
  1. 检查 `clipboard_panel.py` 内部对 clipboard store 的引用，确认类变量为 `self.clipboard_store`。
  2. 将 `self._clip_store` 替换为 `self.clipboard_store`。
- **最终方案**：
  将 `clipboard_panel.py` 中错误引用的属性名称纠正为 `self.clipboard_store`。
- **预防建议**：
  在代码修改和重构后，对于任何涉及全局/底层事件监听的逻辑，必须在各种输入条件（如复制 text/code/link）下进行充分的手动触发测试。

### 问题3：多文件判定代码大量重复
- **问题描述**：为了在历史数据迁移脚本 [migrate_history.py](file:///home/hzb/opencode-switcher/migrate_history.py) 中也进行高精度代码检测，该文件复制了 `clipboard_store.py` 里的 25 个正则以及 `classify_text`/`detect_language_name` 等判定逻辑（共 200+ 行重复代码）。
- **原因分析**：未将共用算法提取为模块级公共函数。
- **解决过程**：
  1. 在 `clipboard_store.py` 中，将 `classify_text` 和 `detect_language_name` 移出 `ClipboardStore` 类，定义为模块级的全局函数。
  2. 在 `ClipboardStore` 类中保留包装方法，保证对外部库的向后兼容。
  3. 修改 `migrate_history.py`，彻底删除重复的正则与函数定义，变更为 `from clipboard_store import classify_text, detect_language_name` 导入。
- **最终方案**：
  重构核心分类器为模块全局函数并在两端共用，彻底消除了冗余代码，并使用 `test_clipboard_classification.py` 进行了全量用例验证。
- **预防建议**：
  如果一个工具类/底层库中包含不依赖实例状态（如 `self`）的纯计算函数，应优先考虑将其设计为模块级别的独立函数，方便其他辅助脚本直接导入使用。

### 问题4：未 strip 的原生字符串对 `CURLY_NEWLINE_RE` 匹配失效导致老旧用例识别失效
- **问题描述**：测试用例 `}\n` 或者是 `{\r\n}` 能够通过老版分类器被匹配为 `code`。但在优化为启发式打分时，该文本被首先 `.strip()` 剥离了首尾所有空格换行符，使正则 `CURLY_NEWLINE_RE` 匹配失效，从而被归为了 `text`。
- **原因分析**：大括号换行属于一种极其特殊的跨行片段，对它的匹配对文本周围的换行格式有强依赖，但分类器入口处的全局 `strip` 破坏了该格式。
- **解决过程**：
  在 `classify_text` 的前半部分设计一个独立的早期快捷退出条件，在对文本进行 `.strip()` 之前（或者通过原生的未 strip 文本 `text`）进行 `CURLY_NEWLINE_RE` 正则检索。
- **最终方案**：
  在 Python/JS 逻辑中，直接使用未被 strip 的 `text` 进行 `CURLY_NEWLINE_RE.search(text)`。
- **预防建议**：
  需要匹配结构化格式、缩进、或包含边界换行特征的正则，必须对未剥离空白（unstripped）的原生字符串执行正则检查。

## 三、技术要点沉淀

- **模块级公共辅助函数设计**：
  将没有实例状态依赖的类方法提取为文件级公共函数，增强了代码的灵活性与可重用性。
- **多端对齐高保真启发式分类算法**：
  通过前缀强特征校验（JSON/HTML/Shebang/CLI）+ 多维度关键字权重分级打分模型，实现高稳定高响应的语言检测。
- **GTK CSS 开发避坑规范**：
  在为 PyGObject/Gtk.Widget 设计 CSS 时，严禁使用复杂的排版属性（如 `text-transform`, `box-shadow` 的多重效果），必须在 Python 业务逻辑层先行进行数据清洗和大小写转换。

## 四、后续优化建议

- **支持更丰富的编程语言**：未来可在打分机制中加入 Markdown、Go、Rust、YML/YAML 等其他常用语言的具体判定与语言标记。
- **性能优化限制**：如果剪切板历史数量非常大，可以在正则检测时，对匹配较慢的正则（如 SQL）采用针对性的大写首字符优先匹配分支等方式进行过滤加速。

## 五、参考资料

- `copyous` 开源项目关于 Highlight.js 启发式打分机制的实现方法。
- `PyGObject` 官方 API 关于样式与 CSS Provider 的限制文档。
