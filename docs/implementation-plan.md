# Bash 工具优化 — 实施计划

## 概述

**目标**：解决 agent 执行 bash 命令时可能卡在 stdin 等待的问题，实现输出活性检测、阶梯式解阻塞、提示模式检测和改进错误信息。

**分支**：`feat/optimize-bash-tool`

**涉及文件**：
| 文件 | 改动规模 | 说明 |
|------|---------|------|
| `tool_registry/bash.py` | ~150 行 | 核心改动，新增检测/解阻塞/错误信息 |
| `tests/test_bash_isolation.py` | ~70 行 | 新增测试用例 |

**本次不涉及（未来再做）**：
- 非阻塞执行（`run_in_background` 模式）— 需新增工具 API，架构变动大
- PTY 支持
- Allowlist 改造

---

## 1️⃣ 改动点全量梳理

### 1.1 `tool_registry/bash.py`（570 行 → 约 720 行）

#### A. 新增常量（文件顶部，现有常量下方）

| 常量名 | 类型 | 值 | 说明 |
|--------|------|-----|------|
| `_STDIN_IDLE_THRESHOLD` | `int` | `15` | 无新输出超过此秒数，判定可能卡在 stdin |
| `_PROMPT_PATTERNS` | `list[tuple[str, str, str]]` | 见下方 | 交互提示模式列表：(模式正则, 匹配说明, 建议) |
| `_UNBLOCK_EOF_WAIT` | `float` | `3.0` | 发送 EOF 后等待恢复的秒数 |
| `_UNBLOCK_SIGINT_WAIT` | `float` | `3.0` | 发送 SIGINT 后等待恢复的秒数 |

**`_PROMPT_PATTERNS` 详细定义**：
```python
_PROMPT_PATTERNS: Final[list] = [
    (r'[Pp]assword\s*[:：]', "密码输入提示", "命令正在请求密码，请使用非交互方式（如通过环境变量或参数传递）"),
    (r'[Yy]es\s*[/:：]?\s*[Nn]o\s*[:：]?\s*$', "Yes/No 确认", "请添加 -y/--yes 等自动确认参数"),
    (r'[Ee]nter\s+(your\s+)?[Cc]hoice', "选择提示", "请通过参数直接指定选项"),
    (r'(?<![\w])[?]\s*$', "问号提示符", "命令正在等待选择确认"),
    (r'[Ss]elect\s+an\s+option', "选项选择", "请通过参数直接指定选项"),
    (r'继续[?）\)]?\s*$', "中文确认提示", "请添加 -y/--yes 等自动确认参数"),
    (r'确认[?）\)]?\s*$', "中文确认提示", "请添加 -y/--yes 等自动确认参数"),
    (r'请输入', "中文输入提示", "请通过参数直接提供输入"),
]
```

---

#### B. 新增辅助函数（`_check_interactive` / `_check_session_breaker` 附近）

**① `_detect_prompt_pattern(output: str) -> Optional[str]`**
- **位置**：`_check_session_breaker` 之后
- **目的**：扫描输出末尾（最后 500 字符）检测交互提示模式
- **逻辑**：
  ```python
  def _detect_prompt_pattern(output: str) -> Optional[str]:
      if not output:
          return None
      tail = output[-500:]  # 只检查末尾 500 字符
      for pattern, label, suggestion in _PROMPT_PATTERNS:
          if re.search(pattern, tail, re.MULTILINE):
              return f"⚠️ 检测到交互提示（{label}）。{suggestion}"
      return None
  ```

**② `_format_stdin_stuck_message(command: str, hint: Optional[str] = None) -> str`**
- **位置**：`_detect_prompt_pattern` 之后
- **目的**：生成标准化的 stdin 阻塞错误信息
- **逻辑**：
  ```python
  def _format_stdin_stuck_message(command: str, tried_eof: bool = False, 
                                   tried_sigint: bool = False,
                                   prompt_hint: Optional[str] = None) -> str:
      parts = [
          "⚠️ 命令被 stdin 阻塞（无新输出超过 15 秒）：",
          f"   命令: {command[:200]}",
      ]
      if prompt_hint:
          parts.append(f"   {prompt_hint}")
      parts.append("")
      parts.append("💡 可能的原因和解决方案：")
      parts.append("   1. heredoc 未正确终止 → 确保结束标记独占一行且前后无空格")
      parts.append('   2. 命令在等待 stdin 输入 → 使用 echo/printf 通过管道传递输入')
      parts.append("   3. 命令在等待交互确认 → 添加 -y/--yes 等自动确认参数")
      parts.append("   4. 命令在请求密码 → 通过环境变量或 --password-file 参数传递")
      parts.append("")
      if tried_sigint:
          parts.append("🔄 已发送 SIGINT 尝试恢复，请重新以非交互方式执行命令")
      elif tried_eof:
          parts.append("🔄 已发送 EOF (Ctrl+D) 尝试恢复，请重新以非交互方式执行命令")
      return "\n".join(parts)
  ```

---

#### C. `_BashSession` 类修改

**① 新增字段 `__init__` 方法**

在 `__init__` 中新增（第 188 行附近）：
```python
self._last_output_time: float = 0.0
self._eof_sent: bool = False
self._sigint_sent: bool = False
```

**② 修改 `execute()` 方法 — 核心改动**

**修改点 1**：在命令发送后的初始化区添加计时变量
```python
# 在 sentinel_cmd_b 发送之后，while 循环之前
last_output_time = time.monotonic()
eof_sent = False
sigint_sent = False
loop_start = time.monotonic()
```

**修改点 2**：轮询循环体内添加活性检测（`poll.poll(50)` 返回空时的处理）
```
原代码 (第 255-257 行)：
    events = poll.poll(50)
    if not events:
        continue

新代码：
    events = poll.poll(50)
    if not events:
        # 无新数据输出，检查是否 idle 过久
        now = time.monotonic()
        idle_duration = now - last_output_time
        cmd_elapsed = now - loop_start
        
        if idle_duration > _STDIN_IDLE_THRESHOLD and not eof_sent:
            # ── Step 1: 关闭 stdin 发送 EOF ──
            eof_sent = True
            try:
                process.stdin.close()
            except (BrokenPipeError, OSError):
                pass
            # 重置 idle 计时，给命令一个机会在 EOF 后完成
            last_output_time = time.monotonic()
            continue
            
        elif idle_duration > _STDIN_IDLE_THRESHOLD and eof_sent and not sigint_sent:
            # ── Step 2: 发送 SIGINT ──
            sigint_sent = True
            try:
                process.send_signal(signal.SIGINT)
            except (ProcessLookupError, OSError):
                pass
            last_output_time = time.monotonic()
            continue
        
        # Step 3: EOF + SIGINT 都没用，继续等待直到超时
        continue
```

**修改点 3**：数据读取后重置 idle 计时
```
原代码 (第 259 行)：
    chunk = os.read(fd, 65536)
    if not chunk:
        break
    output_buf.extend(chunk)

新代码：
    chunk = os.read(fd, 65536)
    if not chunk:
        break
    output_buf.extend(chunk)
    last_output_time = time.monotonic()  # 重置 idle 计时
```

**修改点 4**：超时处理后新增 stdin 阻塞诊断

在 `if not sentinel_found:` 块中（原代码第 281 行附近），修改返回值：

```
原代码：
    if not sentinel_found:
        self._timed_out = True
        self._kill_process_group()
        ...
        return {
            "output": f"命令执行超时（{timeout}秒），session 已终止。\n{output}",
            "exit_code": -1,
            "timed_out": True,
        }

新代码：
    if not sentinel_found:
        # 尝试最后读取一次（可能 EOF/SIGINT 刚生效）
        try:
            remaining = os.read(fd, 65536)
            if remaining:
                output_buf.extend(remaining)
                # 再检查一次 sentinel
                sidx = output_buf.find(sentinel_start)
                if sidx != -1:
                    after = output_buf[sidx:]
                    eidx = after.find(sentinel_end)
                    if eidx != -1:
                        code_bytes = after[len(sentinel_start):eidx]
                        try:
                            exit_code = int(code_bytes.decode("ascii"))
                        except (ValueError, UnicodeDecodeError):
                            exit_code = -1
                    output_buf = output_buf[:sidx]
                    sentinel_found = True
        except OSError:
            pass
    
    if not sentinel_found:
        # 真正的超时，杀进程
        self._timed_out = True
        self._kill_process_group()
        output = output_buf.decode("utf-8", errors="replace").strip()
        ...
        return {
            "output": f"命令执行超时（{timeout}秒），session 已终止。\n{output}",
            "exit_code": -1,
            "timed_out": True,
            "stdin_stuck": True,
        }
```

**修改点 5**：正常返回时附加 stdin 解阻塞备注

在正常返回分支（sentinel 找到后），如果 eof_sent 或 sigint_sent 为 True，在输出中附加诊断信息：

```python
# 在 output 组装后，return 之前
stdin_note = ""
if eof_sent or sigint_sent:
    prompt_hint = _detect_prompt_pattern(output)
    note_parts = ["\n\n⚠️ 检测到命令被 stdin 阻塞"]
    if eof_sent:
        note_parts.append("，已通过 EOF (Ctrl+D) 解阻塞")
    if sigint_sent:
        note_parts.append("，已通过 SIGINT (Ctrl+C) 中断后恢复")
    if prompt_hint:
        note_parts.append(f"\n{prompt_hint}")
    note_parts.append("\n💡 请避免使用交互式命令，或通过参数直接提供输入。")
    stdin_note = "".join(note_parts)

return {"output": output + stdin_note, "exit_code": exit_code, "timed_out": False}
```

**③ 新增 `send_eof()` 方法**
```python
def send_eof(self):
    """向进程 stdin 发送 EOF (关闭 stdin 管道)。"""
    if self.process is not None and self.process.stdin:
        try:
            self.process.stdin.close()
        except (BrokenPipeError, OSError):
            pass
```

**④ 新增 `send_sigint()` 方法**
```python
def send_sigint(self):
    """向进程发送 SIGINT 信号。"""
    if self.process is not None and self.process.pid:
        try:
            self.process.send_signal(signal.SIGINT)
        except (ProcessLookupError, OSError):
            pass
```

---

#### D. `execute_bash()` 函数修改

**修改点 1**：在超时处理分支中增加 stdin 诊断（第 470 行附近）

```
原代码：
    if timed_out:
        close_bash_session(session_key)
        parts = ["⚠️ 命令执行超时，已自动重启 bash session"]
        if output:
            parts.append("")
            parts.append(output)
        return "\n".join(parts)

新代码：
    if timed_out:
        stdin_stuck = result.get("stdin_stuck", False)
        if stdin_stuck:
            prompt_hint = _detect_prompt_pattern(output)
            msg = _format_stdin_stuck_message(
                command, tried_eof=True, tried_sigint=True,
                prompt_hint=prompt_hint,
            )
            close_bash_session(session_key)
            if output:
                return msg + "\n\n" + output
            return msg
        else:
            close_bash_session(session_key)
            parts = ["⚠️ 命令执行超时，已自动重启 bash session"]
            if output:
                parts.append("")
                parts.append(output)
            return "\n".join(parts)
```

---

#### E. TOOL_SCHEMAS 描述更新

**修改 `bash` 工具描述**，在末尾追加：
```
自动检测 stdin 阻塞并尝试 EOF/SIGINT 解阻塞。
```

---

#### F. 新增 import

在文件顶部增加：
```python
import signal
```

---

### 1.2 `tests/test_bash_isolation.py`（64 行 → 约 134 行）

新增以下测试方法：

| 测试方法 | 说明 |
|---------|------|
| `test_stdin_idle_detection_heredoc` | 测试不完整的 heredoc 触发 stdin 检测 |
| `test_stdin_idle_detection_input_cmd` | 测试 `python3 -c "input()"` 触发 stdin 检测 |
| `test_eof_unblocks_command` | 测试 EOF 能解阻塞并保留 session |
| `test_sigint_unblocks_command` | 测试 SIGINT 能解阻塞并保留 session |
| `test_prompt_pattern_detection` | 测试 `_detect_prompt_pattern` 能识别常见提示 |
| `test_session_alive_after_unblock` | 测试 session 在解阻塞后仍可用 |

---

## 2️⃣ 详细实施计划

### 步骤 1：新增 import 和常量（bash.py）

| 项目 | 内容 |
|------|------|
| **位置** | 文件顶部，`import uuid` 之后 |
| **操作** | 添加 `import signal` |
| **位置** | `_CONDITIONAL` 字典下方，`_HARDENED_ENV` 上方 |
| **操作** | 添加 `_STDIN_IDLE_THRESHOLD`、`_PROMPT_PATTERNS`、`_UNBLOCK_EOF_WAIT`、`_UNBLOCK_SIGINT_WAIT` 常量 |
| **风险** | 低，纯新增无副作用 |

### 步骤 2：新增辅助函数（bash.py）

| 项目 | 内容 |
|------|------|
| **位置** | `_check_session_breaker` 函数之后（约第 144 行） |
| **操作** | 添加 `_detect_prompt_pattern()` 和 `_format_stdin_stuck_message()` 两个函数 |
| **风险** | 低，独立函数无副作用 |

### 步骤 3：修改 `_BashSession.__init__`（bash.py）

| 项目 | 内容 |
|------|------|
| **位置** | `__init__` 方法内（第 188 行附近） |
| **操作** | 添加 `self._last_output_time`、`self._eof_sent`、`self._sigint_sent` 三个实例变量 |
| **风险** | 低，仅新增字段 |

### 步骤 4：核心改造 — 修改 `_BashSession.execute()` 轮询循环（bash.py）

| 项目 | 内容 |
|------|------|
| **位置** | `execute()` 方法内的 while 循环（第 238-280 行） |
| **操作** | ① 循环前添加 `last_output_time`/`eof_sent`/`sigint_sent`/`loop_start` 局部变量 |
| | ② `poll.poll(50)` 返回空时，新增 idle 检测和阶梯解阻塞逻辑 |
| | ③ 数据读取后重置 `last_output_time` |
| **风险** | ⚠️ **核心改动**，需确保不破坏正常命令执行路径 |
| **回退策略** | 若该步骤有 Bug，可通过 git revert 回退整个 commit |

### 步骤 5：修改超时处理逻辑（bash.py）

| 项目 | 内容 |
|------|------|
| **位置** | `if not sentinel_found:` 块（第 281-300 行） |
| **操作** | ① 超时前尝试最后一次读取 stdout |
| | ② 如果发现 sentinel，按正常完成处理 |
| | ③ 标记 `stdin_stuck` 字段 |
| **风险** | 中，需确保超时路径仍能正确杀进程 |

### 步骤 6：修改正常返回路径（bash.py）

| 项目 | 内容 |
|------|------|
| **位置** | 正常返回前（第 302-310 行附近） |
| **操作** | 如果 `eof_sent` 或 `sigint_sent`，在 output 中附加解阻塞备注 |
| **风险** | 低，仅影响返回信息格式 |

### 步骤 7：新增 `send_eof()` 和 `send_sigint()` 方法（bash.py）

| 项目 | 内容 |
|------|------|
| **位置** | `_kill_process_group` 方法之后（约第 310 行附近） |
| **操作** | 新增两个方法 |
| **风险** | 低 |

### 步骤 8：修改 `execute_bash()` 超时处理分支（bash.py）

| 项目 | 内容 |
|------|------|
| **位置** | `if timed_out:` 分支（第 460-470 行附近） |
| **操作** | 区分"stdin 阻塞超时"和"正常超时"，前者给出详细诊断 |
| **风险** | 低 |

### 步骤 9：更新 TOOL_SCHEMAS 描述（bash.py）

| 项目 | 内容 |
|------|------|
| **位置** | 文件末尾 `TOOL_SCHEMAS` 列表（第 530 行附近） |
| **操作** | 在 bash 工具描述末尾追加 stdin 检测说明 |
| **风险** | 低 |

### 步骤 10：新增测试用例（test_bash_isolation.py）

| 项目 | 内容 |
|------|------|
| **位置** | 文件末尾，`TestBashIsolation` 类内 |
| **操作** | 新增 6 个测试方法 |
| **风险** | 低，新增测试不影响现有功能 |

---

## 3️⃣ 执行顺序总览

```
步骤 1  ──→  新增 import + 常量
   │
   ▼
步骤 2  ──→  新增辅助函数
   │
   ▼
步骤 3  ──→  修改 _BashSession.__init__
   │
   ▼
步骤 4  ──→  【核心】修改 execute() 轮询循环
   │
   ▼
步骤 5  ──→  修改超时处理逻辑
   │
   ▼
步骤 6  ──→  修改正常返回路径
   │
   ▼
步骤 7  ──→  新增 send_eof() / send_sigint()
   │
   ▼
步骤 8  ──→  修改 execute_bash() 超时分支
   │
   ▼
步骤 9  ──→  更新 TOOL_SCHEMAS
   │
   ▼
步骤 10 ──→  新增测试
```

**注意**：步骤 4-6 是相互关联的，建议作为一次 edit 完成，然后在步骤 10 的测试中进行验证。

---

## 4️⃣ 关键伪代码参考

### 修改后的 `execute()` 核心循环

```python
# 在 while 循环之前初始化
last_output_time = time.monotonic()
eof_sent = False
sigint_sent = False
loop_start = time.monotonic()

while time.monotonic() < deadline:
    # ── 取消检查（不变）──
    if cancel_event and cancel_event.is_set():
        self._kill_process_group()
        ...
        return {"output": ..., "exit_code": -1, "timed_out": False}

    # ── 进程退出检查（不变）──
    if process.poll() is not None and not sentinel_found:
        remaining = os.read(fd, 65536)
        if remaining:
            output_buf.extend(remaining)
        break

    # ── 轮询输出 ──
    events = poll.poll(50)
    
    if not events:
        # ── 无新数据：检查是否 idle 过久 ──
        now = time.monotonic()
        idle_duration = now - last_output_time
        
        if idle_duration > _STDIN_IDLE_THRESHOLD and not eof_sent:
            # Step 1: EOF
            eof_sent = True
            try:
                process.stdin.close()
            except (BrokenPipeError, OSError):
                pass
            last_output_time = now  # reset for next step
            continue
            
        elif idle_duration > _STDIN_IDLE_THRESHOLD and eof_sent and not sigint_sent:
            # Step 2: SIGINT
            sigint_sent = True
            try:
                process.send_signal(signal.SIGINT)
            except (ProcessLookupError, OSError):
                pass
            last_output_time = now
            continue
        
        # Step 3: 继续等待直到超时
        continue
    
    # ── 有数据到达 ──
    chunk = os.read(fd, 65536)
    if not chunk:
        break
    output_buf.extend(chunk)
    last_output_time = time.monotonic()  # 重置 idle 计时
    
    # ── Sentinel 检测（不变）──
    sidx = output_buf.find(sentinel_start)
    if sidx != -1:
        sentinel_found = True
        # ... 提取退出码 ...
        output_buf = output_buf[:sidx]
        break
```

### 超时处理新增逻辑

```python
if not sentinel_found:
    # 尝试最后一次读取（EOF/SIGINT 可能刚生效）
    try:
        remaining = os.read(fd, 65536)
        if remaining:
            output_buf.extend(remaining)
            # 重新检查 sentinel
            sidx = output_buf.find(sentinel_start)
            if sidx != -1:
                # ... 解析退出码 ...
                sentinel_found = True
                output_buf = output_buf[:sidx]
    except OSError:
        pass

if not sentinel_found:
    # 真正的超时
    self._timed_out = True
    self._kill_process_group()
    output = output_buf.decode("utf-8", errors="replace").strip()
    ...
    return {"output": ..., "exit_code": -1, "timed_out": True, "stdin_stuck": eof_sent or sigint_sent}
```

---

## 5️⃣ 风险评估

| 风险 | 概率 | 影响 | 缓解措施 |
|------|------|------|---------|
| idle 检测误触发（命令本身长时间无输出，如 `sleep 30`） | 中 | 低 | sleep 不是交互命令，EOF 对其无影响，SIGINT 会终止它但 agent 本意可能就是要等 |
| `process.stdin.close()` 导致后续命令无法执行 | 低 | 中 | EOF 后 session 仍可用，bash 在 EOF 后重启 stdin 处理子命令 |
| SIGINT 误杀正常命令 | 低 | 低 | 仅限于 idle > 15s 且 EOF 无效的情况，概率低 |
| 兼容性问题 | 低 | 高 | 保持原返回值结构不变，只新增 `stdin_stuck` 字段，向后兼容 |

---

## 6️⃣ 验证标准

1. ✅ `python3 << 'PYEOF'\n...` 不完整 heredoc 能在 15s 内被检测并解阻塞
2. ✅ `python3 -c "input()"` 能在 15s 内被检测并解阻塞
3. ✅ 正常命令（`ls`、`grep`、`sleep 5`）不受影响
4. ✅ session 在 EOF/SIGINT 解阻塞后仍可用
5. ✅ 错误信息明确指示 agent 如何修正
6. ✅ 所有现有测试通过
