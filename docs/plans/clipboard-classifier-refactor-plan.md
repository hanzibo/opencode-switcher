# 阶段三实施计划：抽离 `stores/clipboard_store.py` 中的分类器至 `ai_text_utils/classifier.py`

## 目标概述
`stores/` 目录定位为数据持久化与状态管理（SQLite、JSON 读写、缓存、FIFO 队列等）。
目前 `stores/clipboard_store.py` 头部包含了 ~280 行用于文本/代码类型启发式分类（`classify_text`）、编程语言识别（`detect_language_name`）及 28 个专用正则规则。
本阶段将这部分纯文本分析与特征打分算法抽离至专用的无状态工具包 [`ai_text_utils/classifier.py`](file:///home/hzb/opencode-switcher/ai_text_utils/classifier.py)，实现**存储层与算法层的彻底解耦**。

---

## 1. 改动点全量梳理

| 文件路径 | 修改性质 | 具体修改内容与函数/类 | 依赖关系与潜在影响 |
| :--- | :--- | :--- | :--- |
| [`ai_text_utils/classifier.py`](file:///home/hzb/opencode-switcher/ai_text_utils/classifier.py) | **新建** | 迁移 28 个分类正则表达式、`classify_text(text: str) -> str`、`detect_language_name(text: str) -> Optional[str]` | 无外部模块依赖，纯标准库 `re`, `json`, `typing` |
| [`ai_text_utils/__init__.py`](file:///home/hzb/opencode-switcher/ai_text_utils/__init__.py) | **修改** | 导入并导出 `classify_text`, `detect_language_name` 及 28 个正则常量，更新 `__all__` 和模块 docstring | 为上层提供统一的 `ai_text_utils` 顶层导入入口 |
| [`stores/clipboard_store.py`](file:///home/hzb/opencode-switcher/stores/clipboard_store.py) | **修改** | 1. 移除第 9-45 行 28 个正则定义；<br>2. 移除第 95-295 行 `classify_text` 与 `detect_language_name` 实现；<br>3. `from ai_text_utils.classifier import ...` 并保留模块级 re-export；<br>4. `ClipboardStore` 类的 `classify_text` 和 `detect_language_name` 方法继续委托转发 | 100% 保持对外 API 兼容，既有单元测试与调用方无感 |
| [`views/clipboard_panel.py`](file:///home/hzb/opencode-switcher/views/clipboard_panel.py) | **修改** | 优化导入路径为 `from ai_text_utils import detect_language_name` | 语义更清晰，提升模块独立性 |
| [`system/migrate_history.py`](file:///home/hzb/opencode-switcher/system/migrate_history.py) | **修改** | 优化导入路径为 `from ai_text_utils import classify_text, detect_language_name` | 语义更清晰 |
| [`tests/test_clipboard_store.py`](file:///home/hzb/opencode-switcher/tests/test_clipboard_store.py) | **修改** | 保留既有 `TestClipboardClassification` 测试，同时增加对 `ai_text_utils.classifier` 的直接测试覆盖 | 确保双向导入兼容与单测完整性 |
| [`AGENTS.md`](file:///home/hzb/opencode-switcher/AGENTS.md) | **修改** | 更新模块说明中的分类器位置说明 | 文档与架构事实保持同步 |

---

## 2. 详细分步骤实施计划

### Step 1: 创建分类器独立模块 [`ai_text_utils/classifier.py`](file:///home/hzb/opencode-switcher/ai_text_utils/classifier.py) 与统一导出
- **操作目标**：创建 `ai_text_utils/classifier.py`，完整迁移正则规则与打分算法，并在 `ai_text_utils/__init__.py` 中完成聚合导出。
- **涉及位置**：
  - 新建 `ai_text_utils/classifier.py`；
  - 修改 `ai_text_utils/__init__.py`。
- **关键代码/逻辑**：
  ```python
  # ai_text_utils/classifier.py
  import re
  import json
  from typing import Optional

  # 28 个编译好的分类正则常量
  HTML_START_RE = re.compile(...)
  ...
  CURLY_BRACE_RE = re.compile(...)

  def classify_text(text: str) -> str:
      ...

  def detect_language_name(text: str) -> Optional[str]:
      ...
  ```
- **验证**：`python3 -m py_compile ai_text_utils/classifier.py ai_text_utils/__init__.py`。

---

### Step 2: 重构 [`stores/clipboard_store.py`](file:///home/hzb/opencode-switcher/stores/clipboard_store.py)
- **操作目标**：从 `stores/clipboard_store.py` 剥离分类器实现，转为引入 `ai_text_utils.classifier` 并提供模块级向后兼容重导出。
- **涉及位置**：
  - `stores/clipboard_store.py` 第 9-45 行、第 95-295 行。
- **具体改动**：
  ```python
  from ai_text_utils.classifier import (
      HTML_START_RE, HTML_END_RE, SHEBANG_RE, CLI_CMD_RE, CURLY_NEWLINE_RE,
      BASH_VAR_ASSIGN_RE, BASH_CMD_SUBST_RE, BASH_COND_RE, BASH_KEYWORD_RE, BASH_LOOP_RE,
      PY_DEF_RE, PY_CLASS_RE, PY_IMPORT_RE, CPP_INCLUDE_RE, CPP_DEFINE_RE, CPP_USING_RE,
      TYPED_DECL_RE, JS_CONSOLE_RE, JS_VAR_RE, JS_FUNC_RE, JS_FUNCTION_KW_RE,
      LANG_KEYWORDS_RE, C_COMMENT_RE, SQL_SELECT_RE, SQL_MOD_RE, GENERIC_KW_RE,
      SEMICOLON_RE, CURLY_BRACE_RE,
      classify_text, detect_language_name
  )
  ```
- **验证**：运行 `venv/bin/python3 -m unittest tests.test_clipboard_store`。

---

### Step 3: 更新调用点与测试用例
- **操作目标**：优化 `views/clipboard_panel.py` 和 `system/migrate_history.py` 中的导入，并在 `tests/test_clipboard_store.py` 中验证双向导入有效性。
- **涉及位置**：
  - `views/clipboard_panel.py`
  - `system/migrate_history.py`
  - `tests/test_clipboard_store.py`
- **验证**：运行全量 692 测试 `venv/bin/python3 -m unittest discover tests`。

---

### Step 4: 更新架构文档与代码图谱
- **操作目标**：同步 `AGENTS.md` 中的模块说明，并执行 `codegraph sync` 刷新代码图谱索引。
- **涉及位置**：`AGENTS.md`。
- **验证**：运行 `codegraph sync` 与全量测试。

---

## 3. 风险评估与回退策略

| 风险点 | 风险等级 | 规避与防范措施 | 回退策略 |
| :--- | :---: | :--- | :--- |
| 外部模块或测试依赖 `stores.clipboard_store` 的分类函数/正则 | 低 | 在 `stores/clipboard_store.py` 中完整保留模块级重导出，对外 API 100% 保持兼容 | 如有未发现的隐式依赖报错，直接从 `ai_text_utils` 桥接导出补齐 |
| GNOME Extension 与 Python 端的评分算法不一致 | 零 | 仅移动代码位置，算法逻辑与打分权重（含正则表达式）逐字严格保持一致 | `git diff` 校验算法体字符级无差异 |
