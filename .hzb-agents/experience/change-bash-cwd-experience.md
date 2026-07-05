# 切换 Bash 工作路径命令 开发经验总结

> **分支名**：`add-change-bash-cwd`  
> **开发周期**：2026-07-05 至 2026-07-05  
> **关键词**：`CWD切换` `Bash会话` `命令防注入` `Gtk.FileChooserDialog` `焦点防护`

## 一、经验与教训总结

### 1.1 做得好的地方
- **焦点防御模式的成熟应用**：在创建 `Gtk.FileChooserDialog` 时，自觉继承并实践了前期的焦点钩子保护设计（连接 `show` 和 `destroy` 信号至全局焦点守护函数），完美规避了系统选择器拉起时主面板失去焦点而导致物理自动折叠折消的隐患。
- **稳健的 Shell 命令拼接机制**：在向活跃的 Bash 子进程输入 `cd` 命令时，没有使用简单的字符串拼接，而是选用标准库的 `shlex.quote` 对用户路径进行转义转储，彻底堵截了由于目录包含特殊字符（例如空格、分号、反引号等）产生的命令执行失败或潜在的安全命令注入缺陷风险。

### 1.2 需要改进的地方
- **对持久化会话的外部生命周期容错审查稍显滞后**：首期实现中忽略了当底层 Bash session 处于超时/退出锁定状态下，同步执行 `cd` 指令会抛出 `RuntimeError` 的问题。如果在开发初期就先画好外部 session 状态流转图，就能第一步避免未捕获异常传递给主事件循环的隐患。

## 二、关键问题与解决方案记录

### 问题1：切换工作路径导致主窗口自动隐藏
- **问题描述**：通过 `/cd` 无参数命令触发目录选择弹窗时，系统自带的 `Gtk.FileChooserDialog` 夺取了桌面输入焦点，触发主窗口 `focus-out-event`，引发主面板自动执行 `hide()` 析构并强制连带销毁了弹出的选择器本身。
- **原因分析**：主搜索面板在设计上拥有失焦自动隐藏（折叠）机制，当系统检测到有新 Modal/Window 夺取焦点时会判定失焦，清理未锁定的所有关联子视图。
- **解决过程**：
  ```python
  if self.on_dialog_shown:
      dialog.connect("show", lambda *_: self.on_dialog_shown())
  if self.on_dialog_hidden:
      dialog.connect("destroy", lambda *_: self.on_dialog_hidden())
  ```
- **最终方案**：将对话框显示与销毁动作纳入全局 `_dialog_active` 状态锁定作用域中，使得在文件选择器存在期间强行锁死主面板的隐藏信号触发。
- **预防建议**：今后在 Switcher 内任何拉起 transient 系统或自定义窗口的场景，均必须主动向父层组件申请焦点锁定信号。

### 问题2：若 Bash 处于超时锁死状态下执行 `/cd` 导致抛出异常崩溃
- **问题描述**：当前 Bash 会话此前已发生命令超时（即 `_timed_out` 为 True）时，执行 `/cd` 命令切换路径会直接向 GUI 主线程抛出未捕获的 `RuntimeError` 崩溃堆栈。
- **原因分析**：`_BashSession.execute` 包含状态门栏，如果检测到超时，会主动向上层 raise 异常来强制调用者执行 `restart=True`。而在 `set_bash_cwd` 中调用此接口时，未对该异常做安全兜底。
- **解决过程**：
  在 `set_bash_cwd` 调用 `_bash_session.execute` 处增加 `try...except Exception` 结构保护，捕获异常后将错误信息格式化输出给前端 WebView，而非冒泡扩散。
- **最终方案**：
  ```python
  try:
      cmd = f"cd {shlex.quote(path)}"
      res = _bash_session.execute(cmd, timeout=5)
      ...
  except Exception as e:
      return f"⚠️ 现有 Bash 会话异常（{e}），已更新工作目录配置。新路径：{path}"
  ```
- **预防建议**：凡是调用可能出现堵塞、超时或连接异常的跨进程通信接口时（包括子进程 pipe 和网络 HTTP 等），均应在其最终控制侧进行本地异常隔离兜底。

## 三、技术要点沉淀

- **安全 Shell 命令注入转义**：
  ```python
  import shlex
  safe_cmd = f"cd {shlex.quote(unsafe_path)}"
  ```
- **GTK 原生目录选择器配置与定位**：
  ```python
  dialog = Gtk.FileChooserDialog(action=Gtk.FileChooserAction.SELECT_FOLDER)
  dialog.set_current_folder(initial_path)
  ```

## 四、后续优化建议

- **工作路径同步持久化配置**：当前设置的 CWD 仅在运行期内存生命周期中生效，在下次彻底重启 Switcher 托盘时将重置回默认路径。未来可将最近使用的工作路径追加保存至 `config.json` 配置文件中，实现关机重启后的记忆特性。

## 五、参考资料

- [open-command-fix-experience.md 焦点防护方案经验](file:///home/hzb/opencode-switcher/.hzb-agents/experience/open-command-fix-experience.md)
- [Python shlex.quote 安全转义 API 规范](https://docs.python.org/3/library/shlex.html#shlex.quote)
