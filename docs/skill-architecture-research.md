# Skill 架构调研分析报告

> 调研日期: 2026-07-27
> 分支: research/skill-architecture

---

## 一、行业标准：Agent Skills Open Standard

### 1.1 背景

2025 年 12 月，Anthropic 发布了 **Agent Skills 开放标准**，随后被 30+ 主流 AI 工具采纳，包括：

| 平台 | 技能目录路径 | 调用方式 |
|------|-------------|---------|
| **Claude Code** | `.claude/skills/`、`~/.claude/skills/` | 自动触发 + `/skill-name` 斜杠命令 |
| **OpenAI Codex** | `.agents/skills/`、`~/.agents/skills/` | 自动触发 + `$skill-name` 命令 |
| **OpenClaw** | `~/.openclaw/skills/` | 自动触发 + 可配置斜杠命令 |
| **Gemini CLI** | `.gemini/skills/` | 自动触发 |

### 1.2 标准 SKILL.md 格式

```markdown
---
name: code-review
description: Perform code reviews on repository code. 
  Use when the user asks to review code, check quality, or find bugs.
  Do not use for deployment or testing tasks.
license: MIT
compatibility: git, python >= 3.10
metadata:
  category: development
  complexity: medium
allowed-tools: read_file, grep_search, bash
---

# Code Review Skill

## Steps
1. Check formatting and style
2. Analyze security vulnerabilities
3. Verify error handling
...
```

### 1.3 标准目录结构

```
my-skill/
├── SKILL.md          # 必需：元数据 + 指令
├── scripts/          # 可选：可执行脚本
│   └── analyze.py
├── references/       # 可选：参考文档
│   ├── REFERENCE.md
│   └── style-guide.md
└── assets/           # 可选：模板、资源
    └── report-template.md
```

### 1.4 渐进式加载（Progressive Disclosure）

| 阶段 | 加载内容 | Token 开销 | 触发时机 |
|------|---------|-----------|---------|
| **发现 Discovery** | `name` + `description` | ~100 tokens/个 | 会话启动时 |
| **激活 Activation** | 完整的 SKILL.md 正文 | < 5000 tokens | 任务匹配描述时 |
| **执行 Execution** | scripts/、references/ 等 | 按需加载 | 指令引用时 |

---

## 二、OpenCodeSwitcher 当前架构 vs 行业标准

### 对比总览

| 维度 | 行业标准 (Agent Skills) | OpenCodeSwitcher 当前 | 状态 |
|------|----------------------|---------------------|------|
| **SKILL.md 格式** | YAML frontmatter + Markdown | ✅ YAML frontmatter + Markdown | ✅ 一致 |
| **目录结构** | `name/SKILL.md` + scripts/references/assets | ✅ `name/SKILL.md` 单文件 | ⚠️ 缺少附属目录 |
| **name 字段** | 必填，严格校验 | ✅ 支持 | ✅ |
| **description 字段** | 必填，路由核心 | ✅ 支持 | ✅ |
| **allowed-tools** | 可选，实验性 | ✅ 已支持 | ✅ |
| **license 字段** | 可选 | ❌ 未解析 | ❌ 缺失 |
| **compatibility 字段** | 可选（环境要求） | ❌ 未解析 | ❌ 缺失 |
| **metadata 字段** | 可选（扩展属性） | ❌ 未解析 | ❌ 缺失 |
| **渐进式加载** | Discovery → Activation → Execution | ⚠️ 仅实现了发现阶段 | ⚠️ 部分 |
| **scripts/ 目录** | 可执行脚本 | ❌ 不支持 | ❌ 缺失 |
| **references/ 目录** | 参考文档 | ❌ 不支持 | ❌ 缺失 |
| **assets/ 目录** | 模板资源文件 | ❌ 不支持 | ❌ 缺失 |
| **技能范围 Scope** | Enterprise/Personal/Project/Plugin | ✅ Global/Project 两级 | ⚠️ 较简单 |
| **斜杠命令调用** | `/skill-name` 或 `$skill-name` | ✅ `/skill:name` / `skill:name` | ✅ 良好 |
| **自动触发** | 通过 description 匹配 | ✅ XML 注入系统提示 | ✅ 良好 |
| **单个技能开关** | 无标准规定 | ✅ 设置界面独立开关 | ✅ 领先 |
| **全局技能开关** | 无标准规定 | ✅ enable_global_skills | ✅ 领先 |
| **加载前门控** | OS/二进制/环境变量检查 | ❌ 不支持 | ❌ 缺失 |
| **环境变量注入** | 技能执行期间临时注入 | ❌ 不支持 | ❌ 缺失 |
| **技能注册表** | ClawHub 等公开注册表 | ❌ 无 | ❌ 缺失 |
| **name 校验** | 严格（小写+连字符等） | ❌ 无校验 | ❌ 缺失 |
| **内容大小限制** | < 500 行 / < 5000 tokens | ⚠️ 30000 字符截断 | ⚠️ 较宽松 |

---

## 三、核心优化建议

### 🔴 高优先级

#### 1. 支持完整目录结构（scripts/references/assets）

**现状**: 当前只读取 SKILL.md 单文件，忽略同一目录下的附属资源。

**建议**: 扩展 SkillStore 使其支持技能目录中的 `scripts/`、`references/`、`assets/` 子目录，并在 `read_skill` 工具中暴露这些资源的路径和内容。

**改动量**: 中等（主要在 skill_store.py 和 tool_registry/skill.py）

#### 2. 补充缺失的 frontmatter 字段

**现状**: 只解析 `name`、`description`、`allowed-tools`。

**建议**: 增加对 `license`、`compatibility`、`metadata` 字段的解析，存储在 SkillMetadata 中。

**改动量**: 小

```python
@dataclass
class SkillMetadata:
    name: str
    description: str
    path: str
    allowed_tools: List[str] = field(default_factory=list)
    license: str = ""           # 新增
    compatibility: str = ""     # 新增
    metadata: Dict[str, str] = field(default_factory=dict)  # 新增
```

#### 3. skills.xml 注入的 description 优化

**现状**: 直接将原始 description 注入 XML。行业最佳实践建议 description 应同时回答"做什么"和"何时触发"。

**建议**: 遵循行业规范，描述应包含触发关键词和否定条件（"Do not use for..."），并控制在 512 字符以内。

**改动量**: 小（文档/指南层面的优化）

### 🟡 中优先级

#### 4. 技能范围扩展（Enterprise 级别）

**现状**: 支持 Global 和 Project 两级。

**建议**: 增加系统级 `/etc/opencode-switcher/skills/` 目录（Enterprise/Admin 范围），形成三级：Enterprise → Global → Project。优先级：Project > Global > Enterprise。

**改动量**: 小

#### 5. 加载前门控机制（Load-time Gating）

**现状**: 所有扫描到的技能都展示给 AI。

**建议**: 参考 OpenClaw 的 load-time gating，允许技能声明前置条件（如需要 git、特定 Python 版本、网络访问等），不满足条件的技能不展示给模型。

可在 `compatibility` 字段或 `metadata` 中实现。

**改动量**: 中等

#### 6. SKILL.md 内容大小建议与校验

**现状**: 仅在 UI 层有 30000 字符截断，无结构性建议。

**建议**: 参照行业标准，建议 SKILL.md 正文不超过 500 行 / 5000 tokens，超出部分应移入 references/ 目录。可在 validation 或文档中给出指导。

**改动量**: 小（文档 + 可选校验工具）

### 🟢 低优先级

#### 7. name 字段校验

**现状**: 无校验。

**建议**: 增加命名校验（1-64 字符，小写字母+数字+连字符，不允许首尾连字符、连续连字符），与行业标准对齐。

**改动量**: 小

#### 8. 技能注册表/市场

**现状**: 无。

**建议**: 远期可考虑支持从远程注册表安装技能（类似 npm 或 ClawHub），但目前非核心需求。

**改动量**: 大

#### 9. 环境变量注入

**现状**: 无。

**建议**: 允许技能声明所需的临时环境变量，执行期间注入，执行后恢复。

**改动量**: 中

---

## 四、总结

### OpenCodeSwitcher 的优势（已领先于行业标准）

1. ✅ **Per-skill enable/disable toggle** — 设置界面中每个技能可独立开关，行业标准未规定此功能
2. ✅ **Global skill enable/disable toggle** — 全局技能总开关
3. ✅ **XML 格式的 skill summary 注入** — 结构化的系统提示注入，便于 AI 理解
4. ✅ **read_skill 工具** — 实现了标准的按需读取机制
5. ✅ **/skill 命令体系** — 列表查看和手动触发功能完善

### 最值得优先做的 3 项改进

| 优先级 | 改进项 | 预期收益 |
|--------|--------|---------|
| 🔴 1 | 支持 scripts/references/assets 目录结构 | 技能可以附带脚本和资源文件，能力大幅提升 |
| 🔴 2 | 补充 license/compatibility/metadata 字段 | 与行业标准完全兼容，技能可移植性增强 |
| 🟡 3 | 加载前门控机制 | 避免 AI 尝试在不满足前置条件时调用技能 |

---

*报告结束*
