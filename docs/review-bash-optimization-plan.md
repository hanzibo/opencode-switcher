# 代码审查报告：Bash 工具优化 (feat/optimize-bash-tool)

## 审查范围

| 文件 | 状态 | 改动 |
|------|------|------|
| `tool_registry/bash.py` | ✅ 修改 | +239 / -11 行 |
| `tests/test_bash_isolation.py` | ✅ 修改 | +142 / -1 行 |
| `docs/implementation-plan.md` | ✅ 新增 | 593 行（计划文档，仅辅助） |

---

## 审查总结

整体代码质量良好，结构清晰，测试覆盖充分（20 个测试全部通过）。发现了 **6 个问题**，其中高优先级 2 个，中优先级 2 个，低优先级 2 个。

---

## 问题清单

### 🔴 高优先级（影响正确性）

#### H1. `_check_heredoc` 不支持带引号的 heredoc 定界符

**位置**：`bash.py` 第 216 行，`_check_heredoc` 函数

**问题**：正则 `<<-?\s*(\w+)` 中的 `\w+` 不匹配引号，导致带引号的 heredoc 漏检：

```python
# 以下写法在 bash 中合法，但 _check_heredoc 完全检测不到
python3 << 'PYEOF'   # ← 单引号定界符
python3 << "EOF"     # ← 双引号定界符
```

**影响**：`python3 << 'PYEOF'\n...` 缺结束标记时，预检放行，要靠 15s idle 检测兜底。

**修复方案**：扩展正则，在捕获定界符后剥离外围引号：

```python
_match = re.match(r'<<-?\s*(?:\'(\w+)\'|"(\w+)"|(\w+))', part)
delimiter = next(g for g in _match.groups() if g is not None)
```

或者更简洁的方式：

```python
for match in re.finditer(r"""<<-?\s*(?:'(\w+)'|"(\w+)"|(\w+))""", command):
    delimiter = next(g for g in match.groups() if g)
```

**预期收益**：消除带引号 heredoc 的预检盲区。

---

#### H2. `_check_heredoc` 可能误判字符串字面量中的 `<<`

**位置**：`bash.py` 第 216 行

**问题**：正则 `<<-?\s*(\w+)` 会匹配字符串中的 `<<`，导致误报：

```bash
echo "x << EOF"        # 不是 heredoc，但会被 _check_heredoc 捕获
```

**修复方案**：由于 bash 语法解析本身的复杂性（字符串、转义、嵌套引用），完美区分需要真正的 bash 解析器。务实方案：只检查 `<<` 是否出现在行首或 `;`/`|`/`&&`/`||` 之后，减少误判：

```python
# 简单改进：要求 << 前只能是行首、空白、或命令连接符
if not re.search(r'(?:^|[;&|()])\s*)<<-?\s*(...)', command, ...):
```

但这样仍不完美。另一个思路：不修复误报，因为误报最多只是让用户看到一个"不完整 heredoc"的错误提示，不会造成实际损害，且这种情况在实际使用中极少见。

**建议**：暂时标记为已知限制，不修改。如果后续出现误报投诉再处理。

---

### 🟡 中优先级（影响可维护性）

#### M1. 死代码：三个已定义但未使用的变量

**位置**：
- `bash.py` 第 134-139 行：`_UNBLOCK_EOF_WAIT` 和 `_UNBLOCK_SIGINT_WAIT`
- `bash.py` 第 389 行：`loop_start`

**问题**：
- `_UNBLOCK_EOF_WAIT = 3.0` 和 `_UNBLOCK_SIGINT_WAIT = 3.0` 定义了等待时间常量，但实际代码中等待是隐式的（继续轮询直到超时或 sentinel 出现），并没有使用这些常量。
- `loop_start = time.monotonic()` 被赋值但从未被读取。

**修复方案**：删除未使用的变量。3 行删除。

```python
# 删除以下行
_UNBLOCK_EOF_WAIT: Final[float] = 3.0
_UNBLOCK_SIGINT_WAIT: Final[float] = 3.0
# 以及 execute() 中的
loop_start = time.monotonic()
```

**预期收益**：消除代码噪声，减少维护者的认知负担。

---

#### M2. Sentinel 解析逻辑重复 3 次

**位置**：`bash.py` 中 `execute()` 方法的 3 个位置

**问题**：sentinel 查找和退出码解析的代码段（约 10 行）在以下位置完全重复：

1. **主 while 循环内**（第 443-454 行）— 正常执行路径
2. **超时后的"最后读取"**（第 461-477 行）— 超时恢复路径
3. 严格来说是 2 次，但第 2 次是第 1 次的完整复制

```python
# 重复的代码段：
sidx = output_buf.find(sentinel_start)
if sidx != -1:
    sentinel_found = True
    after = output_buf[sidx:]
    eidx = after.find(sentinel_end)
    if eidx != -1:
        code_bytes = after[len(sentinel_start):eidx]
        try:
            exit_code = int(code_bytes.decode("ascii"))
        except (ValueError, UnicodeDecodeError):
            exit_code = -1
    output_buf = output_buf[:sidx]
```

**修复方案**：提取为内部方法 `_parse_sentinel(output_buf, sentinel_start, sentinel_end) -> tuple[bool, int, bytearray]`：

```python
def _parse_sentinel(self, output_buf: bytearray,
                    sentinel_start: bytes, sentinel_end: bytes
                    ) -> tuple[bool, int, bytearray]:
    """查找 sentinel 标记并解析退出码。返回 (found, exit_code, cleaned_buf)。"""
    sidx = output_buf.find(sentinel_start)
    if sidx == -1:
        return False, -1, output_buf
    after = output_buf[sidx:]
    eidx = after.find(sentinel_end)
    code = -1
    if eidx != -1:
        try:
            code = int(after[len(sentinel_start):eidx].decode("ascii"))
        except (ValueError, UnicodeDecodeError):
            code = -1
    return True, code, output_buf[:sidx]
```

**预期收益**：消除重复逻辑，一处修改处处生效，降低维护成本。

---

### 🔵 低优先级（代码风格/优化建议）

#### L1. 硬编码魔法数字

**位置**：`bash.py` 多处

**问题**：
| 值 | 位置 | 语义 |
|----|------|------|
| `500` | `_detect_prompt_pattern` | 输出尾部检查长度 |
| `50` | 轮询循环 | poll 超时毫秒 |
| `65536` | 多处 `os.read` | 读取缓冲区大小 |
| `200` | `_format_stdin_stuck_message` | 命令截断长度 |

**建议**：提取为模块级常量。但考虑到这些值在短期内不会变化，且都有直观语义，优先级较低。

#### L2. `_PROMPT_PATTERNS` 使用裸元组

**位置**：`bash.py` 第 142-157 行

**问题**：每个模式条目是 `(regex, label, suggestion)` 三元组，通过 `pattern, label, suggestion = entry` 解包。如果未来需要增加字段（如 `severity`），所有解包处都需要修改。

**建议**：使用 `NamedTuple` 或 `dataclass`：

```python
from typing import NamedTuple

class PromptPattern(NamedTuple):
    pattern: re.Pattern
    label: str
    suggestion: str
```

**预期收益**：类型安全，扩展友好。

---

## 分步修改方案

### 步骤 1：修复 H1 — 支持带引号的 heredoc 定界符

**文件**：`tool_registry/bash.py`，`_check_heredoc` 函数（第 216 行）

**改动**：
```python
# 旧
for match in re.finditer(r'<<-?\s*(\w+)', command):
    delimiter = match.group(1)
    ...

# 新
for match in re.finditer(r"""<<-?\s*(?:'(\w+)'|"(\w+)"|(\w+))""", command):
    delimiter = next(g for g in match.groups() if g)
    ...
```

**验证**：新增测试 `test_heredoc_complete_quoted` 和 `test_heredoc_incomplete_quoted`

### 步骤 2：删除死代码 M1

**文件**：`tool_registry/bash.py`

**改动**：
- 删除第 134-139 行的 `_UNBLOCK_EOF_WAIT` 和 `_UNBLOCK_SIGINT_WAIT`
- 删除第 389 行的 `loop_start = time.monotonic()`

### 步骤 3：提取 sentinel 解析辅助方法 M2

**文件**：`tool_registry/bash.py`，`_BashSession` 类

**改动**：
- 新增 `_parse_sentinel()` 内部方法
- 替换 2 处重复的 sentinel 解析代码

### 步骤 4：应用 L1/L2 优化

**文件**：`tool_registry/bash.py`

**改动**：
- 为 500/50/65536/200 定义模块级常量
- 为 `_PROMPT_PATTERNS` 引入 `PromptPattern` NamedTuple

### 步骤 5：补充测试

**文件**：`tests/test_bash_isolation.py`

**改动**：
- 新增带引号定界符的 heredoc 测试
- 新增字符串中 `<<` 不误报的测试

---

## 验证方法

1. 运行完整测试：`python3 -m unittest tests.test_bash_isolation -v`
2. 手动测试带引号的 heredoc：
   ```bash
   python3 << 'EOF'
   print("hello")
   EOF
   ```
3. 测试不完整的带引号 heredoc 被预检拦截

## 回滚思路

所有改动均为增量修改，不影响现有测试。如有问题：
- `git log --oneline` 查看最近提交
- `git revert <commit>` 回滚即可
