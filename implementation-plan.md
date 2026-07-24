# 多会话隔离与 Skill 手动触发（/skill）重构实施计划

## 一、 改动点梳理

结合当前系统的多会话并发 Bash 隔离设计，解决先前 `session_key` 隐式退回 `"default"` 导致的状态错位问题，实现：
1. **显式会话 Context 绑定**：前端 UI（Popovers、`/skill`、`/cd`）统一显式向 `get_bash_cwd()` 传递当前激活的 `conversation_id`。
2. **新会话 CWD 继承**：开启新对话时，新会话自动继承当前活跃会话的 CWD 路径，避免重复 `/cd`。
3. **会话生命周期闭环**：删除对话时显式清理并终止对应的 `_BashSession` 子进程。
4. **`/skill` 自动补全与手动触发**：实现两列显示格式（`skill:<name>` + `[u] <description>`）及发送 `/skill:<name>` 自动载入执行。

### 涉及文件清单：

| 序号 | 文件路径 | 修改类型 | 需修改函数/类/方法 | 具体修改内容 |
|---|---|---|---|---|
| 1 | `ai_popovers.py` | 修改 | `AICommandPopover` 类 | 增加 `conversation_id` 属性支持；在 `rebuild()` 中显式向 `get_bash_cwd()` 传递会话 ID |
| 2 | `ai_chat_panel.py` | 修改 | `AIChatPanel` 类 | 1. 传递 `self._ai_conversation_id` 给 `AICommandPopover`<br/>2. `_start_new_conversation()` 中继承上个会话的 CWD<br/>3. 删除对话时调用 `close_bash_session(conv_id)` |
| 3 | `tool_registry/_state.py` | 修改 | `_BashState.get_cwd()` | 优先从 `/proc/{pid}/cwd` 动态读取实时物理路径，未匹配到 key 时继承上一有效 CWD |
| 4 | `tool_registry/bash.py` | 修改 | `get_bash_cwd()` | 确保正确路由显式传入的 `session_key` |
| 5 | `tests/test_bash_isolation.py` | 新增 | 测试用例类 | 增加多会话 Bash 隔离、CWD 继承、生命周期释放及 `/skill` 补全的单测 |

---

## 二、 详细实施计划

### 步骤 1：重构 `tool_registry/_state.py` 与 `bash.py` 路径路由
- **目标**：保证 CWD 的存取精确归属于传入的 `session_key`，且支持从 Linux `/proc/{pid}/cwd` 实时感知物理路径。
- **具体改动**：
  ```python
  # tool_registry/_state.py
  def get_cwd(self, key: str) -> str:
      session = self._sessions.get(key)
      if session and hasattr(session, "process") and session.process and session.process.poll() is None:
          try:
              real_cwd = os.readlink(f"/proc/{session.process.pid}/cwd")
              if os.path.isdir(real_cwd):
                  self._cwds[key] = real_cwd
                  self.cwd = real_cwd
                  return real_cwd
          except Exception:
              pass
      return self._cwds.get(key, self.cwd)
  ```

---

### 步骤 2：重构 `ai_popovers.py` 关联 `conversation_id`
- **目标**：在输入 `/skill` 弹出自动补全弹窗时，准确使用当前对话关联的 CWD 扫描可用 Skills。
- **具体改动**：
  ```python
  # ai_popovers.py
  class AICommandPopover(Gtk.Popover):
      def __init__(self, relative_to_entry, command_list, conversation_id_fn=None):
          ...
          self.conversation_id_fn = conversation_id_fn

      def rebuild(self, prefix: str):
          ...
          conv_id = self.conversation_id_fn() if self.conversation_id_fn else None
          cwd = tool_registry.get_bash_cwd(session_key=conv_id)
          skills = SkillStore().get_skills(cwd=cwd)
  ```

---

### 步骤 3：在 `ai_chat_panel.py` 中强化 CWD 继承与生命周期
- **目标**：解决会话切换、新建及删除时的 CWD 状态错位与子进程泄露。
- **具体改动**：
  1. 初始化 `AICommandPopover` 时传入 `conversation_id_fn=lambda: self._ai_conversation_id`。
  2. 在 `start_new_conversation()` 中：
     ```python
     prev_cwd = tool_registry.get_bash_cwd(session_key=self._ai_conversation_id)
     self._reset_ai_panel_silent()
     tool_registry.set_bash_cwd(prev_cwd, session_key=self._ai_conversation_id)
     ```
  3. 在删除对话时（`/delete` 或 `delete_conversation`）：
     ```python
     tool_registry.close_bash_session(conv_id)
     ```

---

### 步骤 4：编写自动化单元测试 `tests/test_bash_isolation.py`
- **目标**：确保多会话路径隔离、CWD 继承、物理路径同步及 `/skill` 补全完全稳定。
- **测试覆盖**：
  - 测试 `conv_A` 与 `conv_B` 拥有独立 CWD 及 Bash 进程。
  - 测试创建新会话时 CWD 自动继承。
  - 测试删除会话时 Bash 进程释放。

---

## 三、 风险与回退策略
1. **进程释放风险**：关闭 Session 时使用 `try-except` 包裹 `close_bash_session`，防止异常导致 UI 卡死。
2. **回退策略**：本次改动集中在 `ai_popovers.py` 与 `ai_chat_panel.py` 的 CWD 传参，若需回退可轻松切换回简单单例调用。
