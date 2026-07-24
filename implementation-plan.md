# Skill 特性（方案 A：标准 SKILL.md 渐进式注入）实施计划

## 一、 改动点梳理

根据确认的 **方案 A（标准 SKILL.md 渐进式注入）**，整体架构分为：
1. `SKILL.md` 目录发现与 YAML Frontmatter 解析（`skill_store.py`）
2. 内置 `read_skill` AI 工具注册（`tool_registry/skill.py` + `tool_registry/__init__.py`）
3. System Prompt 动态注入 `<available_skills>` 摘要（`ai_tool_loop.py`）
4. UI 工具卡片渲染映射（`ai_text_utils/render.py`）
5. 单元测试全覆盖（`tests/test_skill_store.py`）

本次改动涉及 **6 个代码文件**：

| 序号 | 文件路径 | 文件类型 | 核心修改内容 | 依赖与影响范围 |
|---|---|---|---|---|
| 1 | `skill_store.py` | 新增文件 | 实现 `SkillMetadata` 数据结构、`SkillStore` 目录扫描解析器与 Prompt 格式化 | 依赖标准库 `os`, `pathlib`, `re`, `yaml`/轻量正则解析；无破坏性影响 |
| 2 | `tool_registry/skill.py` | 新增文件 | 定义 `read_skill` 工具 Schema 及 `execute_read_skill` 执行逻辑 | 依赖 `skill_store.py` |
| 3 | `tool_registry/__init__.py` | 修改文件 | 导入 `skill` 模块并打包 `TOOL_DEFINITIONS` 与 `TOOL_EXECUTORS` | 注册新工具 `read_skill`，无破坏性影响 |
| 4 | `ai_tool_loop.py` | 修改文件 | 在循环开始前调用 `SkillStore` 动态将技能元数据摘要注入 System Prompt | 增强 LLM 上下文，无破坏性影响 |
| 5 | `ai_text_utils/render.py` | 修改文件 | 在 `_TOOL_DISPLAY_FIELD` 映射中增加 `"read_skill": "skill_name"` | 优化 WebView 中工具步的摘要展示 |
| 6 | `tests/test_skill_store.py` | 新增文件 | 针对 YAML 解析、路径发现、Prompt 拼接及工具调用的单测 | 保证测试覆盖率与稳定性 |

---

## 二、 详细实施计划

### 步骤 1：创建 Skill 存储与解析器 `skill_store.py`
- **目标**：实现 `~/.config/opencode-switcher/skills/` 与 `.opencode/skills/`（或当前 bash cwd）目录中 `SKILL.md` 的扫描、YAML Frontmatter 解析及 Prompt 摘要生成。
- **改动文件**：`skill_store.py`（新增）
- **核心逻辑**：
  ```python
  @dataclass
  class SkillMetadata:
      name: str
      description: str
      path: str
      allowed_tools: List[str] = field(default_factory=list)

  class SkillStore:
      def get_skills(self, cwd: Optional[str] = None) -> List[SkillMetadata]:
          # 扫描 ~/.config/opencode-switcher/skills/ 及 cwd 下的 .opencode/skills/
          ...

      def get_skills_prompt_summary(self, cwd: Optional[str] = None) -> str:
          # 生成 <available_skills> XML 结构摘要注入 System Prompt
          ...

      def get_skill_content(self, skill_name: str, cwd: Optional[str] = None) -> Optional[str]:
          # 读取特定 SKILL.md 的完整 Markdown 内容
          ...
  ```
- **预估行数**：~120 行

---

### 步骤 2：新增 `read_skill` 工具与注册 `tool_registry/`
- **目标**：为 AI Assistant 提供调取技能详细指南的内置 Tool `read_skill`。
- **改动文件**：
  - `tool_registry/skill.py`（新增，~40 行）
  - `tool_registry/__init__.py`（修改，~10 行）
- **具体改动**：
  1. 在 `tool_registry/skill.py` 中声明 `TOOL_SCHEMAS`：
     ```python
     TOOL_SCHEMAS = [{
         "type": "function",
         "function": {
             "name": "read_skill",
             "description": "按名称读取特定技能（Skill）的完整指导文档和步骤说明。",
             "parameters": {
                 "type": "object",
                 "properties": {
                     "skill_name": {
                         "type": "string",
                         "description": "技能名称（匹配 available_skills 列表中指定的名称）"
                     }
                 },
                 "required": ["skill_name"]
             }
         }
     }]
     ```
  2. 实现 `execute_read_skill(skill_name: str, cancel_event=None)` 函数。
  3. 在 `tool_registry/__init__.py` 中引入 `from . import skill`，并在 `TOOL_DEFINITIONS` 与 `TOOL_EXECUTORS` 中注册 `read_skill`。

---

### 步骤 3：在 ReAct 循环中注入 Skill 提示词 `ai_tool_loop.py`
- **目标**：在发起多轮 LLM 调用前，将当前工作区下可用的 Skills 摘要嵌入 System Prompt。
- **改动文件**：`ai_tool_loop.py`（修改，~15 行）
- **改动位置**：`run_llm_react_loop()` 函数启动处（约 L175-185）。
- **具体逻辑**：
  ```python
  from skill_store import SkillStore

  # 动态注入 Skill 摘要
  current_cwd = tool_registry.get_bash_cwd()
  skill_summary = SkillStore().get_skills_prompt_summary(cwd=current_cwd)
  if skill_summary:
      messages.append({
          "role": "system",
          "content": skill_summary
      })
  ```

---

### 步骤 4：增强 UI 渲染支持 `ai_text_utils/render.py`
- **目标**：在 AI 对话 WebView 界面中，使 `read_skill` 工具调用的摘要行清晰显示技能名称。
- **改动文件**：`ai_text_utils/render.py`（修改，~2 行）
- **具体改动**：
  在 `_TOOL_DISPLAY_FIELD` 字典中添加：
  ```python
  _TOOL_DISPLAY_FIELD = {
      ...
      "read_skill": "skill_name",
  }
  ```

---

### 步骤 5：单元测试验证 `tests/test_skill_store.py`
- **目标**：编写完备的自动化测试，确保 YAML Frontmatter 解析正确、多路径覆盖无误以及工具调用的正常运行。
- **改动文件**：`tests/test_skill_store.py`（新增，~90 行）
- **测试覆盖**：
  - 测试标准 `SKILL.md` 的 YAML 头部解析（`name`, `description`）。
  - 测试全局与项目本地 Skills 的覆盖与合并逻辑。
  - 测试 `get_skills_prompt_summary()` 输出的 XML 格式正确性。
  - 测试 `read_skill` 工具调用的执行结果。

---

## 三、 风险与回退策略
1. **YAML 解析兼容性风险**：优先使用标准 Python 简易 Frontmatter 解析逻辑（截取 `---` 包含块），不依赖外部复杂 C 扩展，如解析失败降级为使用文件名作为 `name`，确保主线程不崩溃。
2. **回退策略**：所有修改均为增量式扩展，若有异常可一键还原 `tool_registry/__init__.py` 与 `ai_tool_loop.py` 的注册调用。
