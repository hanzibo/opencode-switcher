# 整体性能优化 开发经验总结

> **分支名**：`improve-feature-overall-performance`  
> **开发周期**：2026-06-14 至 2026-06-15  
> **关键词**：`性能优化` `异步加载` `进程检索` `列表过滤` `GTK3` `PyGObject`

## 一、经验与教训总结

### 1.1 做得好的地方
- **全方位瓶颈诊断与重构**：精准识别并解决了 UI 主线程三大阻塞来源（`/proc` 慢速扫描、SQLite 与目录探测的同步调用、Gtk 部件的高频物理重建），极大提升了 switcher 面板交互的流畅度。
- **动态列表过滤实践**：成功应用了 `Gtk.ListBox` 的动态过滤机制，替代物理性的 `_rebuild` 重建，实现了秒级过滤响应，减少了大量的 GObject 内存申请与销毁开销。
- **健全的代码审查流程**：在合并前开展了详尽的代码走查，成功拦截了 `pgrep` 异常穿透问题与多线程加载竞态问题。

### 1.2 需要改进的地方
- **对命令行工具边界状态考虑不足**：初期只测试了 `pgrep` 成功匹配的情况，忽略了其无匹配进程时会返回状态码 `1` 并抛出 Python 异常的特性，导致了隐性的慢速 `/proc` 遍历回退。
- **并发环境下的状态保护意识需加强**：将阻塞操作移至后台线程时，应第一时间联想到多次高频触发对同一 UI 资源的渲染覆盖问题，并预先设计好 sequence 保护。

## 二、关键问题与解决方案记录

### 问题1：pgrep 未匹配进程时返回状态码 1 导致优化失效
- **问题描述**：在没有 opencode 会话运行的常态下，每次打开面板仍伴有轻微延迟，经查发现后台依然在执行慢速的 `/proc` 目录全量扫描。
- **原因分析**：`pgrep` 命令在找不到匹配进程时，其退出状态码为 `1`。`subprocess.check_output` 认为这是命令失败而抛出 `CalledProcessError`，之前的通用 `except Exception` 捕获该异常后，直接回退执行了 `/proc` 全量遍历，导致优化被穿透。
- **解决过程**：重构 `[_detect_live_sessions](file:///home/hzb/opencode-switcher/session_store.py#L26)` 的异常控制流，优先捕获 `subprocess.CalledProcessError`，并针对 `returncode == 1` 的正常无匹配情况，直接令 `pids = []`；仅在其他错误时回退。
- **最终方案**：
  ```python
  try:
      out = subprocess.check_output(["pgrep", "-f", "opencode"], stderr=subprocess.DEVNULL)
      pids = out.decode("utf-8", errors="ignore").strip().split()
  except subprocess.CalledProcessError as e:
      if e.returncode == 1:
          pids = []
      else:
          pids = _fallback_pids()
  except Exception:
      pids = _fallback_pids()
  ```
- **预防建议**：调用系统外部 CLI 命令作为核心逻辑时，务必测试并妥善处理“无结果/无匹配”（通常为状态码 1）和“命令不存在”（通常为 `FileNotFoundError`）等边界状态。

### 问题2：快速开关面板导致多后台线程异步加载的竞态条件 (Race Condition)
- **问题描述**：频繁快速连按快捷键开关面板时，UI 数据偶尔发生错乱，加载出了陈旧的会话列表。
- **原因分析**：每次面板触发 `on_panel_opened` 都会新建一个 `threading.Thread` 进行数据库与文件读取，并在完成时通过 `GLib.idle_add` 渲染至 UI。由于线程调度与 SQLite 锁等待时间不同，先启动的线程可能在较迟时完成，从而用陈旧的数据覆盖了最新线程的结果。
- **解决过程**：在 `App` 类中引入自增的加载序列号标记 `self._session_load_seq`。在启动线程前将其捕获，并作为参数传入线程；在 `GLib.idle_add` 触发的闭包中比较当前序列号，只在两者一致时才更新 UI。
- **最终方案**：
  ```python
  self._session_load_seq = getattr(self, "_session_load_seq", 0) + 1
  seq = self._session_load_seq

  def _bg_load(seq_val):
      try:
          sessions = get_sessions()
          def _apply():
              if seq_val == self._session_load_seq:
                  self._panel.load_sessions(sessions)
              return False
          GLib.idle_add(_apply)
      except Exception as e:
          print(f"Error loading sessions in background: {e}", flush=True)

  threading.Thread(target=_bg_load, args=(seq,), daemon=True).start()
  ```
- **预防建议**：异步 UI 渲染在处理高频触发时，必须保证一次加载请求的生命周期唯一性，防止旧数据乱序覆盖新数据。

### 问题3：剪切板搜索时高频物理重建 Gtk.ListBox 行部件引起界面卡顿
- **问题描述**：当剪切板项达到上限（例如 150 条）时，用户在搜索框输入文字会导致渲染闪烁、卡顿及打字迟滞。
- **原因分析**：每次搜索词改变都会调用 `_rebuild()` 销毁旧的 `Gtk.ListBoxRow` 部件并全量重新分配/实例化新的子 Widget。高密度的 GObject 创建/销毁给 GTK 渲染主线程和垃圾回收器带来了极大压力。
- **解决过程**：在面板初始化时绑定 `Gtk.ListBox.set_filter_func`，将数据一次性全部构建挂载为列表行。在用户键入搜索词时，仅更新关键字并调用 `invalidate_filter()`，使 GTK 内部高效隐藏或显示现有行。
- **最终方案**：
  ```python
  self._content_list.set_filter_func(self._list_filter_func, None)
  
  def set_filter(self, query: str):
      self._filter_query = query.strip().lower()
      self._content_list.invalidate_filter()
      GLib.idle_add(self._select_first_visible_row)
  ```
- **预防建议**：在频繁的用户交互事件（如 `changed`）中，切忌进行复杂的 UI 物理重建。应当优先利用容器自带的 Visible/Filter 过滤器。

## 三、技术要点沉淀

- **高性能进程检索**：`pgrep -f <pattern>` 相比遍历 `/proc` 并读取每个 `cmdline` 文件，效率提升了近百倍。在 Linux 桌面应用开发中，应作为检索指定 PID 的首选工具，并保留原生 `/proc` 遍历作为兜底。
- **异步请求 Sequence 保护模式**：后台线程配合 `GLib.idle_add` 使用时，利用自增序列号 `seq_val == self._latest_seq` 进行校验，是简单且极其有效的防竞态重叠模式。
- **GTK 行可见性过滤器**：通过 `Gtk.ListBox.invalidate_filter()` 在 UI 线程内直接重绘行可见性，规避了大量对象申请与垃圾回收开销。

## 四、后续优化建议

- **长连接与数据库连接池**：目前 `session_store.py` 在 `get_sessions` 中每次都会新建连接，并配置 `PRAGMA journal_mode=WAL`。如果性能遇到极高频瓶颈，可以考虑在主应用进程中常驻一个 SQLite 连接以削减建连开销。
- **使用 inotify 进行进程生命周期被动监听**：代替主动的 `/proc` 扫描，未来可结合系统的进程事件套接字或监听，真正做到零 I/O 扫描。

## 五、参考资料

- [PyGObject Gtk.ListBox API Docs](https://pygobject.readthedocs.io/en/latest/bindings/gtk/gtk_list_box.html)
- [proc(5) — Linux manual page](https://man7.org/linux/man-pages/man5/proc.5.html)
- [AGENTS.md (GTK Thread Safety Rules)](file:///home/hzb/opencode-switcher/AGENTS.md)
