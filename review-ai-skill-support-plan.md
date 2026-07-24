# 代码质量审查与优化计划 (review-ai-skill-support-plan.md)

## 一、 审查摘要

针对分支 `feat-ai-skill-support` 实现的 **AI Skill 机制、/skill 手动检索与补全弹窗、多会话 CWD 隔离与物理路径同步** 特性，进行了覆盖 12 个文件的全量代码质量审查（包括可读性、可维护性、健壮性、性能、安全性、一致性）。

代码整体架构清晰，完全符合 GTK3 桌面规范与 `AGENTS.md` 约定的编码标准，24 项单元测试全部通过。本次审查共识别出 **4 处优化点**（无高风险高优先级阻塞项，3 处中优先级，1 处低优先级）。

---

## 二、 问题清单与优化方案

### 🟡 中优先级（改进可维护性、健壮性与性能）

#### 1. [M-1] `ai_popovers.py` 与 `_state.py` 中的静默异常捕获（Robustness & Maintainability）
- **问题位置**：
  - [ai_popovers.py:79](file:///home/hzb/opencode-switcher/ai_popovers.py#L79)
  - [tool_registry/_state.py:41](file:///home/hzb/opencode-switcher/tool_registry/_state.py#L41)
- **问题描述**：使用 `except Exception: pass` 静默吞掉了所有的异常，导致文件读取失败或进程权限异常时难以调试。
- **改进方案**：引入 `logging.getLogger(__name__)`，将非预期异常记录为 debug 级别日志，或显式捕获特异性异常（`FileNotFoundError`, `ProcessLookupError`）。
- **预期收益**：提升系统的可维护性与故障定位效率。

#### 2. [M-2] `SkillStore` 增加轻量级磁盘 I/O 缓存（Performance）
- **问题位置**：[skill_store.py](file:///home/hzb/opencode-switcher/skill_store.py)
- **问题描述**：用户在输入框打字 `/skill:...` 时，每个字符的变动都会触发 `SkillStore().get_skills(cwd)` 进行磁盘目录扫描。
- **改进方案**：在 `SkillStore` 内部引入 2 秒 TTL (Time-To-Live) 内存缓存，相同 `cwd` 且在有效期内直接从内存返回技能列表。
- **预期收益**：消除连续打字时的重复磁盘 I/O。

#### 3. [M-3] `_handle_skill_command` 的超大 Prompt 防护（Robustness）
- **问题位置**：[ai_chat_panel.py:3025](file:///home/hzb/opencode-switcher/ai_chat_panel.py#L3025)
- **问题描述**：若用户调取的 `SKILL.md` 包含极其庞大的文本（如误放大型 log），直接填入 GTK Entry 可能会造成界面短暂停顿。
- **改进方案**：对读取到的 `content` 增加 30,000 字符防爆卡顿截断保护。
- **预期收益**：保障极端文件下的桌面 UI 流畅度。

---

### 🟢 低优先级（代码风格与解析增强）

#### 4. [L-1] `_parse_frontmatter` 的列表解析增强（Consistency & Robustness）
- **问题位置**：[skill_store.py:115](file:///home/hzb/opencode-switcher/skill_store.py#L115)
- **问题描述**：解析 `allowed-tools` 时，若包含 YAML 数组括号（如 `[read_file, bash]`），原分割可能带有 `[` 或 `]`。
- **改进方案**：去除方括号字符后再进行 split。
- **预期收益**：提升 YAML Frontmatter 兼容性。

---

## 三、 分步修改与验证方案

| 步骤 | 操作目标 | 涉及文件 | 预估代码行数 | 验证方法 |
|---|---|---|---|---|
| Step 1 | 替换静默异常为 Debug 日志与特异性捕获 | `ai_popovers.py`, `tool_registry/_state.py` | ~8 行 | 运行现存单测 + 查看日志 |
| Step 2 | 在 `SkillStore` 中实现 2秒 TTL 缓存 | `skill_store.py` | ~15 行 | 运行 `test_skill_store.py` |
| Step 3 | 增加 `allowed-tools` 方括号清理与超大文本截断 | `skill_store.py`, `ai_chat_panel.py` | ~10 行 | 运行单元测试 |

---

## 四、 回滚思路

若优化过程中出现非预期影响：
- 所有改动均为局部防范性优化，不改变核心 API 契约，可通过 `git checkout -- <file>` 单文件迅速还原。
