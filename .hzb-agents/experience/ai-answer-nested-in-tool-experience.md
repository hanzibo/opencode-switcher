# 修复正式回答有概率嵌入工具调用结果容器内 开发经验总结

> **分支名**：`fix-feature-ai-answer-nested-in-tool`  
> **开发周期**：2026-07-04 至 2026-07-04  
> **关键词**：`AI看盘` `排版嵌套` `Markdown编译隔离` `预处理占位` `正则定位`

## 一、经验与教训总结

### 1.1 做得好的地方
- **占位防护思想应用成功**：借鉴了原有的数学公式保护（Math Escaping）思想，通过临时替换占位符（Placeholder Escape）完全把工具 HTML 盒子从 Markdown 编译环境隔离，从根本上杜绝了输入文本中的未闭合格式串污染上下文的可能。
- **边界条件考虑周全**：利用正则在反替换时自动清洗可能被 markdown 模块自动加塞的 `<p>` 标签，确保页面生成的 DOM 结构绝对合规。

### 1.2 需要改进的地方
- **前期时序理解出现偏差**：在收到用户 `确认` 消息时，误以为是授权直接修改代码，忽视了原本审查流程中“需要用户在代码审查通过后方可执行改动”的流程约定。未来必须严格按分支要求在审查通过后再提交改动。

## 二、关键问题与解决方案记录

### 问题1：工具返回数据中含未闭合 markdown 代码围栏（三反引号）导致页面标签嵌套受损
- **问题描述**：当读取 Markdown 格式文件（如 `git-command-guide.md`）因字数截断留下了奇数个反引号 ` ``` ` 时，`markdown.markdown()` 将折叠盒外侧的闭合标签 `</pre></div>` 及后面的 Assistant 正式回答当成了代码块的内部文本，使回答被强行吞噬进折叠框中。
- **原因分析**：Markdown 编译器是基于行全局扫描的，对于含有破坏性结构标签的 HTML 文本无法保证块完整性。
- **解决过程**：
  1. 在工具容器尾部追加特征注释 `<!-- tool-result-marker -->`。
  2. 在 `ai_text_utils.py` 的 `_markdown_to_html_safe` 前后加入 `_escape_tool_results` 和 `_unescape_tool_results`。
  3. 通过 `(?:^|\n)(...)(?=\n|$)` 行首行尾锚定，实现精准的替换与还原。
- **最终方案**：预编译占位隔离保护。
- **预防建议**：当把包含不受信任的用户或工具大段字符串组装进 HTML/Markdown 进行混合编译时，必须先将整个 HTML 节点隔离逃逸，防止相互干扰。

## 三、技术要点沉淀

- **Markdown 隔离模式（Placeholder Escape Pattern）**：
  在富文本编辑器中，这是一种极其有效的防御型代码模式：
  ```python
  pattern = re.compile(r'(?:^|\n)(<div class="tool-result-box">.*?<!-- tool-result-marker -->)(?=\n|$)', re.DOTALL)
  ```
  该匹配能保证只抓取真实的工具盒子，而不会匹配到任何包含缩进、代码说明文本等其他普通字符，非常适用于混合 HTML/Markdown 渲染。

## 四、后续优化建议

- **通用 HTML 元素保护器**：若后续继续新增其他复杂的 HTML 盒子（如图片轮播等），可以将本占位隔离逻辑重构成通用的 `HtmlBlockEscaper` 类。

## 五、参考资料

- python-markdown 官方说明文档：https://python-markdown.github.io/
- 历史修复经验文件：[math-streaming-robust-experience.md](file:///home/hzb/opencode-switcher/.hzb-agents/experience/math-streaming-robust-experience.md)
