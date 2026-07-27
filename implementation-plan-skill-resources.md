# 实施计划：Skill 机制方案 A（Frontmatter 扩展 + 附属资源文件支持）

> **目标**：全面支持 Agent Skills 开放标准，扩展 `SkillMetadata` 的 `license`、`compatibility`、`metadata` 属性解析，并增加对 Skill 目录下的 `scripts/`、`references/` 和 `assets/` 附属资源文件自动探测与加载感知。

---

## 一、改动点全量梳理

| 序号 | 文件路径 | 修改类型 | 需修改函数/类/方法 | 具体修改内容 | 模块依赖与潜在影响 |
|---|---|---|---|---|---|
| 1 | `skill_store.py` | 修改 | `SkillMetadata` 类 | 新增 `license: str = ""`、`compatibility: str = ""`、`metadata: Dict[str, str]`、`resources: Dict[str, List[str]]` 字段。 | 无负面影响。数据扩展，完全向下兼容。 |
| 2 | `skill_store.py` | 修改 | `_parse_skill_file()` | 1. 提取 `license`、`compatibility` 和 `metadata` 自定义字段；<br>2. 新增 `_scan_skill_resources(dir_path)` 函数，自动扫描 `scripts/`、`references/` 和 `assets/` 中的存在文件。 | 依赖文件系统扫描，仅对文件夹形式的 Skill 生效，单文件 `.md` Skill 兼容不受影响。 |
| 3 | `skill_store.py` | 修改 | `get_skills_prompt_summary()` | 在生成的 System Prompt XML 中，为每个 `<skill>` 增加 `<compatibility>` 和 `<resources>` 节点（如有）。 | 大模型能前置感知所需环境和关联脚本，提高命中率。 |
| 4 | `tool_registry/skill.py` | 修改 | `execute_read_skill()` | 在 `read_skill` 输出顶部追加 `- 许可协议:`、`- 兼容要求:`、`- 附属资源:` 列表展示。 | Agent 调用时能一步到位看到可调用的 `scripts/` 脚本和 `references/` 参考文档路径。 |
| 5 | `tests/test_skill_store.py` | 修改 | `TestSkillStore` 类 | 增加 `test_frontmatter_extended_fields()` 和 `test_skill_subdirectory_resources()` 单元测试。 | 确保新解析与资源扫描逻辑 100% 覆盖。 |

---

## 二、详细实施计划步骤

### 步骤 1：扩展 `SkillMetadata` 结构与 `frontmatter` 解析 (`skill_store.py`)
- **操作目标**：解析 Frontmatter 中的 `license`、`compatibility` 以及未知的自定义 `metadata` 字典。
- **改动位置**：`skill_store.py` Line 15 ~ Line 80
- **伪代码/关键逻辑**：
  ```python
  @dataclass
  class SkillMetadata:
      name: str
      description: str
      path: str
      allowed_tools: List[str] = field(default_factory=list)
      license: str = ""
      compatibility: str = ""
      metadata: Dict[str, str] = field(default_factory=dict)
      resources: Dict[str, List[str]] = field(default_factory=dict)
  ```

### 步骤 2：实现 `scripts/` / `references/` / `assets/` 子目录自动探测 (`skill_store.py`)
- **操作目标**：当 Skill 为目录形式时（即 `base_dir/skill-name/SKILL.md`），自动探测其同级目录下的子资源文件。
- **改动位置**：`skill_store.py` 内新增 `_scan_skill_resources(skill_dir: str) -> Dict[str, List[str]]`
- **伪代码/关键逻辑**：
  ```python
  def _scan_skill_resources(skill_dir: str) -> Dict[str, List[str]]:
      res = {}
      for sub in ["scripts", "references", "assets"]:
          sub_path = os.path.join(skill_dir, sub)
          if os.path.isdir(sub_path):
              files = [os.path.join(sub, f) for f in os.listdir(sub_path) if not f.startswith(".")]
              if files:
                  res[sub] = sorted(files)
      return res
  ```

### 步骤 3：增强 System Prompt XML 摘要生成 (`skill_store.py`)
- **操作目标**：向 `<available_skills>` XML 中注入 `<compatibility>` 和 `<resources>` 信息。
- **改动位置**：`skill_store.py` `get_skills_prompt_summary()`

### 步骤 4：增强 `read_skill` 的资源展现格式 (`tool_registry/skill.py`)
- **操作目标**：在 `read_skill` 输出顶部将探测到的 `scripts/`、`references/` 和 `assets/` 文件列出。
- **改动位置**：`tool_registry/skill.py` `execute_read_skill()`

### 步骤 5：单元测试验证与回归测试 (`tests/test_skill_store.py`)
- **操作目标**：新增完整测试用例并运行 Discover 跑通。
- **测试指令**：`venv/bin/python3 -m unittest discover tests`

---
