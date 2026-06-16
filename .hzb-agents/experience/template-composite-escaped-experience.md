# 动态模板复合语法与转义字符支持 开发经验总结

> **分支名**：`improve-feature-smart-template-prompt-fill`  
> **开发周期**：2026-06-16 至 2026-06-16  
> **关键词**：`动态模板` `复合语法` `转义字符` `占位符清洗` `智能降级` `GTK`

## 一、经验与教训总结

### 1.1 做得好的地方
- **正则表达式前瞻/后瞻与转义融合**：采用负向后瞻 `(?<!\\)` 与包含可选转义字符的序列 `((?:[^}=]|\\:|\\=)+)`，成功将原生冒号与等号的转义匹配完美融入单条正则表达式中，降低了多状态机分步解析的复杂度。
- **清洗函数机制集中设计**：通过设计并复用 `_unescape_template_field` 辅助清洗函数，将解析出的字段（包含 `\:` 和 `\=` 的文本）在渲染到 UI 对话框（`placeholders`/`defaults` 字典）及生成最终复制文本时进行同步解密，实现了词法解析与业务展示的清晰解耦。
- **直接复制场景占位符智能降级（Fallback to Blank）**：当进行直接复制时，能准确根据是否存在默认值（`default_text is not None`）进行分支处理——含默认值时保留并解密默认值，无默认值时将 `${1}` 或 `${1:提示}` 自动抹除替换为 `""`，极大地简化了用户的粘贴后处理流程。
- **测试用例驱动设计**：在 `scratch` 临时工作目录中建立了完备的测试脚本 `test_placeholder_composite.py`，覆盖了纯提示符、纯默认值、复合冒号/等号转义、空格保留、空默认值、直接复制降级等多维度场景，确保每一次调整都有强大的自动化校验保障。

### 1.2 需要改进的地方
- **未提早引入转义清洗函数**：在最初的第一步设计中，未能充分识别到从正则表达式中捕获的原始文本（如 `root\:admin`）会直接原样被扔进 `placeholder` 渲染和 input 初始文本中，导致界面包含额外的 `\` 转义字符，增加了后续微调的步骤。对于转义系统，应当从一开始就建立“解密后渲染”的意识。
- **对非 Raw 格式 Docstring 的语法警告识别不及时**：在编写 `_unescape_template_field` 的 docstring 时，未注意其包含的 `\:` 与 `\=` 反斜杠需要使用原始字符串修饰符 `r""`，引起 Python 编译器的语法警告。这提示我们应当在每次提交前都进行 `python3 -m py_compile` 扫描。

## 二、关键问题与解决方案记录

### 问题1：占位符内转义字符泄露到 UI 界面展示
- **问题描述**：在模板中包含转义符号（如 `${1:用户名\:和\=等号=root\:admin}`）时，弹出的 Dynamic Copy 对话框的输入框默认预填内容中显示为了 `root\:admin`，输入框的占位灰色虚字也显示为 `用户名\:和\=等号`，界面有视觉噪点。
- **原因分析**：正则提取了匹配组后，数据结构 `defaults` 和 `placeholders` 直接使用了捕获组返回的原始字符串，未剔除其中作为转义标记使用的反斜杠。
- **解决过程**：编写了 `_unescape_template_field` 辅助函数，将 `\\:` 替换为 `:`，`\\=` 替换为 `=`。在 `_show_dynamic_copy_dialog` 循环解析提取组时，对 `prompt_text` 和 `default_text` 应用该方法，实现清洗后载入 UI。
- **最终方案**：
  ```python
  if prompt_text:
      placeholders[num] = self._unescape_template_field(prompt_text)
  if default_text:
      defaults[num] = self._unescape_template_field(default_text)
  ```
- **预防建议**：当定义含有转义约定的自定义微型语法结构时，解析出的内容在面向最终用户展示（UI、控件、剪贴板）时必须通过独立的清理映射（Unescape / Decoupling Cleaner）解除转义，以实现“内部处理”与“界面展示”的隔离。

### 问题2：原生冒号与等号误被认作语法分隔符
- **问题描述**：当模板定义类似于 `${1:提示=a=b}` 或 `${1:a:b=c}` 这种 prompt 或 default 中含有原生 `:` 或 `=` 时，原正则表达式 `r"\$\{(\d+)(?::((?:[^}=]|\\:|\\=)+))?(?:=([^}]*))?\}"` 会因为简单的字符排除或分割匹配导致解析错乱（例如把第二个等号后的部分认作独立属性，或者截断匹配）。
- **原因分析**：原正则未放开对转义组的处理，且默认值的匹配前缀 `=` 仅采用简单的零宽度排除，无法区分被转义的等号。
- **解决过程**：使用非贪婪匹配和包含转义排除集的序列 `((?:[^}=]|\\:|\\=)+)` 作为 prompt 的正则段，并使用负向后瞻 `(?<!\\)(?:=([^}]*))?` 控制默认值开头的等号，保证只有当 `=` 前面不是反斜杠 `\` 时才认为是默认值分隔符。
- **最终方案**：
  ```python
  TEMPLATE_REGEX = re.compile(r"\$\{(\d+)(?::((?:[^}=]|\\:|\\=)+))?(?<!\\)(?:=([^}]*))?\}")
  ```
- **预防建议**：针对多分隔符层叠嵌套文本的正则提取，如果引入转义序列，正则部分必须配合“后瞻（Lookbehind）”或“非分隔符组+转义组”的复合交替选择，从而阻断正常分隔符匹配的扩散。

### 问题3：直接复制模板时残留底层占位符表达式
- **问题描述**：用户在双击模板或者右键 Copy 直接复制时，对于没有默认值的占位符（如 `${1}` 或 `${1:代码库绝对路径}`），系统原样将其复制到了剪切板中，用户粘贴后必须手动删除这些技术符号。
- **原因分析**：旧有的 `_process_template_text` 逻辑只是将没有默认值的占位符简单规格化为 `${1}`，没有进行“清除”操作，偏离了自动化效率的初衷。
- **解决过程**：利用正则表达式捕获组 3（默认值）的特性，若其为 `None` 则说明该占位符纯粹是输入指引，在直接复制时无需保留，直接返回 `""`（空字符串）进行替换。
- **最终方案**：
  ```python
  def _process_template_text(self, text: str) -> str:
      def repl(match):
          default_text = match.group(3)
          if default_text is not None:
              return self._unescape_template_field(default_text)
          return ""
      return TEMPLATE_REGEX.sub(repl, text)
  ```
- **预防建议**：宏/占位符替换系统的降级处理（Fallback）应当以用户最终生产消费场景为导向。纯输入性引导标记（如无默认值的 `${index:prompt}`）在非交互式直接输出时，自动降级为“空白”是体验最佳的实践。

## 三、技术要点沉淀

- **基于正则负向后瞻的条件拆解**：
  - 正则表达式中 `(?<!\\)` 能够在词法匹配时有效拦截转义修饰过的特定字符（例如只匹配未转义 of `=` 作为属性边界）。
- **Python Docstring 的 SyntaxWarning 规避**：
  - 在 docstring 中如含有转义字符（如 `\:`），必须使用 raw string 形式 `r"""..."""` 声明，否则 Python 会发出 `SyntaxWarning: invalid escape sequence` 的不兼容警告。
- **模板多阶段 unescape 统一策略**：
  - 代码片段（[clipboard_panel.py](file:///home/hzb/opencode-switcher/clipboard_panel.py)）：
    ```python
    def _unescape_template_field(self, val: Optional[str]) -> Optional[str]:
        r"""Unescape backslash-escaped colons (\:) and equals (\=) inside a template field."""
        if val is None:
            return None
        return val.replace("\\:", ":").replace("\\=", "=")
    ```

## 四、后续优化建议

- **支持完整的反斜杠自转义**：
  - 当前方案仅替换了 `\:` 和 `\=`。若未来出现需在内容中展示字面反斜杠加冒号（如 `\\:`），需要引入通用的转义协议，使 `\\` 能被 unescape 为单个 `\`。
- **解析错误日志记录**：
  - 在处理一些不规范模板时（如转义符号不闭合、语法错乱），可以考虑在 debug 模式下输出警告到 `run.log`，方便用户对自己的高级自定义占位模板进行调试。

## 五、参考资料

- [Python re — Regular expression operations](https://docs.python.org/3/library/re.html)
- [Python raw string literal docs](https://docs.python.org/3/reference/lexical_analysis.html#string-and-bytes-literals)
