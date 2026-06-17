# 剪切板内容分类机制优化 开发经验总结

> **分支名**：`improve-feature-clipboard-classification`  
> **开发周期**：2026-06-17 至 2026-06-17  
> **关键词**：`剪切板分类` `启发式打分` `正则匹配` `多端对齐` `GJS正则兼容性`

## 一、经验与教训总结

### 1.1 做得好的地方
- **多端逻辑的高保真对齐**：保证了 Python 端运行期（[clipboard_store.py](file:///home/hzb/opencode-switcher/clipboard_store.py)）、Python 端数据迁移（[migrate_history.py](file:///home/hzb/opencode-switcher/migrate_history.py)）以及 GNOME Shell JavaScript 扩展端（[extension.js](file:///home/hzb/opencode-switcher/gnome-extension/extension.js)）的分类逻辑 100% 对齐，避免了由于跨语言分类算法不一致导致的前后端数据分类冲突。
- **混合分类策略（绝对前缀匹配 + 启发式打分）**：采用绝对匹配与启发式相结合的策略，既保证了像 JSON, HTML, Shebang, 典型命令行这类强模式片段的绝对快速匹配，又保证了如 Python, C/C++, Java, JS/TS, SQL 等复杂代码段能根据语法符号和关键字的多维特征进行精确加权判定，实现高识别率与低误判率。
- **老旧 JavaScript 引擎正则兼容处理**：在 GNOME Shell 扩展端（基于 GJS/SpiderMonkey）排除了对可变长度后行断言等非主流正则语法的使用，采用固定宽度的后行断言来识别分号（`/(?<!\&[a-zA-Z0-9]{2,6});\s*$/gm`），保障了 GNOME Shell 扩展的绝对稳定性与兼容性。

### 1.2 需要改进的地方
- **前期分析需要将特殊语法孤岛纳入基准**：最初设计的启发式模型忽略了单侧花括号与换行组合（如代码片段的最后一行 `}\n`）或普通大括号块的匹配场景，导致在运行老版测试用例时出现了未判定成功的问题。后续通过补充 `CURLY_NEWLINE_RE` 在未剥离换行文本（unstripped text）上的早期判断，融合了新旧逻辑的优点，才使测试用例完全通过。未来在做此类重构优化前，应提早在分析阶段把所有历史测试用例梳理完备。

---

## 二、关键问题与解决方案记录

### 问题1：启发式得分设计中长篇普通英文（如 import, class）的防误判平衡
- **问题描述**：某些编程语言 of 常用关键字（如 Python 的 `import`, `from`, `class`；C++ 的 `using`；Java/JS 的 `class`）如果直接作为强无条件词出现在纯文本中，有可能在非代码段中累积过高的分数导致错判。
- **原因分析**：没有对关键字做合理的语义隔离与正则边界限定。
- **解决过程**：
  1. 通过正则表达式增加边界限制 `\b` 词界定位。
  2. 调低通用或高频英语词汇（如通用 class 声明）在打分器中的基础权重，将其移至 `GENERIC_KW_RE`（+2 分）或要求它必须搭配其他专有声明模式（如 Python 类声明必须有冒号 `PY_CLASS_RE`；Java 必须有权限限定修饰 `TYPED_DECL_RE`）才能获得更高的独立分（+4 ~ +5 分）。
  3. 为 `export` 与 `function` 划分不同分值：极其不易被文本混淆的 `function` 独立赋予 +3 分，而可能单独出现的 `export` 赋予 +2 分。
- **最终方案**：
  细化了特定模式的评分权重（Class, Function, Keywords 分类评分），将打分阈值定为 4 分，使得代码片段极易跨过阈值，而普通含词文本绝难积累过 4 分。
- **预防建议**：
  在设计文本 analysis 打分机制时，应充分考虑高频天然词汇（Natural Language Keywords）的影响，使用组合正则或边界分级权重隔离技术。

### 问题2：lone curly braces（孤立/空大括号）在文本 strip 后失去换行符导致误判
- **问题描述**：在旧版检测脚本中，测试用例 `}\n` 或者是 `{\r\n}` 能够匹配大括号换行被归为 `code`。但是在优化为启发式打分时，该文本被首先 `.strip()` 剥离了首尾所有空格换行符，使正则 `CURLY_NEWLINE_RE` 匹配失效，从而被归为了 `text`。
- **原因分析**：大括号换行属于一种极其特殊的跨行片段，对它的匹配对文本周围的换行格式有强依赖，但分类器入口处的全局 `strip` 破坏了该格式。
- **解决过程**：
  在 `classify_text` 的前半部分设计一个独立的早期快捷退出条件，在对文本进行 `.strip()` 之前（或者通过原生的未 strip 文本 `text`）进行 `CURLY_NEWLINE_RE` 正则检索。
- **最终方案**：
  在 Python/JS 逻辑中，直接使用未被 strip 的 `text` 进行 `CURLY_NEWLINE_RE.search(text)`。
- **预防建议**：
  需要匹配结构化格式、缩进、或包含边界换行特征的正则，必须对未剥离空白（unstripped）的原生字符串执行正则检查。

---

## 三、技术要点沉淀

- **跨平台/多语言双端分类算法设计规范**：
  设计双端（Python + JavaScript）分类正则和逻辑时，需要以最小公共正则特性集为标准（例如，避免在 JavaScript 中使用复杂的 Python 正则特性，反之亦然）。
- **Python / JS 双端高精度分类核心实现**：
  - **Python 代码打分段**：
    ```python
    # 3.5 Curly Braces with Newline Check (typical of block-structured code)
    if CURLY_NEWLINE_RE.search(text):
        return "code"
    
    # 4. Code Heuristics Scorer
    # ... (detailed scorers for Python, CPP, Java, JS, SQL, Semicolon, Curly Braces)
    ```
  - **JavaScript 代码打分段**：
    ```javascript
    // 3.5 Curly Braces with Newline Check
    if (/[\{\}]\s*[\r\n]|[\r\n]\s*[\{\}]/.test(text)) {
        return "code";
    }
    ```

---

## 四、后续优化建议

- **模型分类器的极限提效**：若后期剪切板历史极长，可以对频繁的正则匹配做预先的大小写不敏感优化（JS 的 `/i`，Python 的 `re.I`）或基于剪切板首字符进行快速跳过分支。
- **支持更多语种识别**：例如 Markdown 语法（块引用 \`\`\` 等）的直接提取与打标展示。

---

## 五、参考资料

- copyous 开源剪切板扩展中 Highlight.js 对代码识别的分值计算方法。
- GJS API 手册及 Mozilla SpiderMonkey 引擎 RegExp 特性限制文档。
