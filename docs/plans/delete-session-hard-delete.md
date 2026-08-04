# delete_session 硬删除改造 — 完善后执行计划

> 基于子代理评审（2 高优先级 + 9 中 + 若干低）修订。状态：**待用户确认**。

## 一、目标

`delete_session` 从纯软删除（`time_archived`）改为：**opencode 官方命令硬删除优先 + 软删除兜底**，且连续删除并发安全（单 worker 串行队列）。

## 二、子代理评审发现的问题（已全部纳入修订）

### 🔴 高优先级（必修）
1. **worker 无异常兜底** → `delete_session` 抛异常会杀死 worker 线程，队列滞留、UI 无反馈
   → 修订：`DeleteQueue._worker_loop` 循环内 `try/except`，异常记入 errors、继续消费、`finally` 清理 worker 引用
2. **`subprocess.run(text=True)` 严格解码** → opencode 输出含非 UTF-8 字节抛 `UnicodeDecodeError`（不被现有 except 捕获）
   → 修订：`text=True, errors="replace"`（Python 3.6+ subprocess.run 支持 errors 参数）

### 🟡 中优先级（修订）
3. **批次边界竞态**：worker `break` 后、置 None 前入队条目滞留
   → 修订：`except queue.Empty` 分支 `if not self._queue.empty(): continue`
4. **seq 与 `_session_load_seq` 不联动**：面板打开时的在途后台加载会覆盖删除刷新（已删会话"诈尸"）
   → 修订：删除刷新前 `self._session_load_seq += 1` 使在途加载失效
5. **主线程同步 `get_sessions()`（含 pgrep）卡 UI**
   → 修订：刷新复用 `_on_panel_opened` 的后台线程 + idle 模式
6. **测试用例 1 断言不可实现**（mock 返回 0 不会真删临时库行）
   → 修订：mock `side_effect` 模拟 CLI 删除效果，或断言"返回 None 且 time_archived 保持 NULL"
7. **`_show_delete_summary` 未定义、`_show_error` 标题硬编码不可复用**
   → 修订：新增 `_show_delete_summary`（WARNING 框，正文 ≤3 条 + 手动清理指引）
8. **CLI 成功后无交叉验证**（CLI 静默 no-op 被误判为已删除）
   → 修订：`returncode==0` 后 `_session_exists()` 复查，行仍在则走软删兜底
9. **确认框文案过度承诺**（"permanently deleted" 与兜底矛盾）
   → 修订："will be permanently deleted from the database. If deletion fails, it will be archived and hidden."

### 🔵 低优先级（采纳）
10. import 修正：`session_store.py` 已有 `subprocess`（只需新增 `shutil`）；`main.py` 需新增 `import queue`
11. 软删 UPDATE rowcount=0 检查（`_soft_delete` 返回影响行数提示）
12. live 会话删除确认框加强提示
13. 取消删除对话框后复位 `panel._delete_in_progress`
14. WAL/VACUUM：**不采纳**（opencode 拥有 DB，VACUUM 需独占锁，保持现状）
15. 测试文件已全部被 git 跟踪（AGENTS.md 描述过时，本次不额外处理）

## 三、改动清单

### 文件 1：`stores/session_store.py`（修改，约 +45 行）

```python
def delete_session(session_id: str) -> Optional[str]:
    """硬删除优先（opencode 官方命令实际删库），失败回退软删除兜底。

    返回 None = 已从数据库彻底删除；
    返回 str = 警告（已软删除隐藏，数据可能残留）或错误（完全未删）。
    """
    if not os.path.isfile(DB_PATH):
        return "Database not found"
    opencode = shutil.which("opencode")
    if opencode:
        try:
            proc = subprocess.run(
                [opencode, "session", "delete", session_id],
                capture_output=True, text=True, errors="replace", timeout=30,
            )
            if proc.returncode == 0:
                if not _session_exists(session_id):
                    return None          # ✅ 交叉验证通过：彻底删除
                hard_err = "CLI 返回成功但 session 仍存在"
            else:
                hard_err = (proc.stderr or proc.stdout or "").strip()[:200]
        except (subprocess.TimeoutExpired, OSError) as e:
            hard_err = str(e)
    else:
        hard_err = "opencode 不在 PATH"
    soft_err = _soft_delete(session_id)
    if soft_err:
        return f"硬删除失败（{hard_err}），且软删除也失败：{soft_err}"
    return f"警告：opencode 删除失败（{hard_err}），已软删除隐藏（数据可能残留）"


def _session_exists(session_id: str) -> bool:
    """交叉验证：CLI 声称成功后确认行是否真的消失。"""
    try:
        conn = sqlite3.connect(DB_PATH, timeout=5)
        try:
            n = conn.execute("SELECT COUNT(*) FROM session WHERE id=?", (session_id,)).fetchone()[0]
            return n > 0
        finally:
            conn.close()
    except Exception:
        return True   # 查不到视为存在，走兜底（保守）


def _soft_delete(session_id: str) -> Optional[str]:
    """软删除兜底：置 time_archived（原 delete_session 逻辑，抽为单点）。"""
    if not os.path.isfile(DB_PATH):
        return "Database not found"
    try:
        conn = sqlite3.connect(DB_PATH, timeout=5)
        conn.execute("PRAGMA journal_mode=WAL")
        try:
            now = int(time.time() * 1000)
            cur = conn.execute(
                "UPDATE session SET time_archived=? WHERE id=?", (now, session_id))
            conn.commit()
            if cur.rowcount == 0:
                return "session 不存在或已被删除（UPDATE 影响 0 行）"
            return None
        finally:
            conn.close()
    except Exception as e:
        return str(e)
```
- 新增 import：`shutil`（`subprocess` 已有）
- `delete_session` 逻辑分层：硬删 → 交叉验证 → 软删兜底 → 分级返回

### 文件 2：`stores/delete_queue.py`（新增，约 70 行，可单测）

```python
"""并发安全删除队列：单 worker 串行消费，批次边界安全，异常自愈。

设计（基于子代理评审修订）：
- 单 worker 串行消费：避免多个 opencode 进程并发写同一 SQLite 的锁竞争
- queue.get(timeout=IDLE_TIMEOUT)：2 秒无新任务自动收尾批次
- 批次边界：break 前复查队列，杜绝"worker 退出窗口入队滞留"
- 异常自愈：单条失败不杀线程，计入 errors 继续消费，finally 清理引用
"""
import queue
import threading
from typing import Callable, List, Optional


class DeleteQueue:
    IDLE_TIMEOUT = 2.0

    def __init__(self, do_delete: Callable[[str], Optional[str]],
                 on_refresh: Callable[[], None],
                 on_batch_done: Callable[[List[str]], None]):
        self._do_delete = do_delete          # (session_id) -> None/err
        self._on_refresh = on_refresh        # 每删一条后调用（主线程 idle 包装由调用方做）
        self._on_batch_done = on_batch_done  # 批次结束错误汇总
        self._queue: queue.Queue = queue.Queue()
        self._worker: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._errors: List[str] = []

    def enqueue(self, session_id: str) -> None:
        """入队并确保 worker 存活（幂等，锁内检查防竞态）。"""
        with self._lock:
            self._queue.put(session_id)
            if self._worker is None or not self._worker.is_alive():
                self._worker = threading.Thread(target=self._worker_loop, daemon=True)
                self._worker.start()

    def _worker_loop(self) -> None:
        try:
            while True:
                try:
                    session_id = self._queue.get(timeout=self.IDLE_TIMEOUT)
                except queue.Empty:
                    # 批次边界：退出前复查，用户刚入队的任务不滞留（评审🟡3）
                    if not self._queue.empty():
                        continue
                    break
                try:
                    err = self._do_delete(session_id)
                except Exception as e:   # 评审🔴1：异常自愈，不杀线程
                    err = f"删除异常：{e}"
                if err:
                    self._errors.append(err)
                self._queue.task_done()
                self._on_refresh()
        finally:
            if self._errors:
                errs = list(self._errors)
                self._errors.clear()
                self._on_batch_done(errs)
            with self._lock:
                self._worker = None
```

### 文件 3：`main.py`（修改，约 +35 行）

```python
# import 区新增：import queue（若不用 queue 直接用 DeleteQueue 则无需）；from stores.delete_queue import DeleteQueue

# __init__ 内新增：
self._session_load_seq = 0   # 若未在 __init__ 初始化需补（_on_panel_opened 用 getattr 兜底）
self._delete_queue = DeleteQueue(
    do_delete=lambda sid: delete_session(sid),
    on_refresh=lambda: self._schedule_refresh_after_delete(),
    on_batch_done=lambda errs: GLib.idle_add(self._show_delete_summary, errs),
)

def _on_delete_session(self, session):
    """确认框 → 入队（worker 串行执行，不阻塞 UI）。"""
    def on_confirm(dialog, response):
        dialog.destroy()
        if response != Gtk.ResponseType.YES:
            # 取消时复位 panel 删除保护标志（评审🔵13）
            if hasattr(self._panel, "_delete_in_progress"):
                self._panel._delete_in_progress = False
            return
        self._delete_queue.enqueue(session.id)
    dialog = Gtk.MessageDialog(
        transient_for=None, modal=True,
        message_type=Gtk.MessageType.QUESTION,
        buttons=Gtk.ButtonsType.YES_NO,
        text=f'Delete "{session.title}"?',
    )
    dialog.format_secondary_text(
        "This session will be permanently deleted from the database. "
        "If deletion fails, it will be archived and hidden from the list.")   # 评审🟡9
    dialog.connect("response", on_confirm)
    dialog.show_all()

def _schedule_refresh_after_delete(self):
    """删除后刷新：使在途后台加载失效 + 后台线程加载（评审🟡4/🟡5）。"""
    self._session_load_seq = getattr(self, "_session_load_seq", 0) + 1
    seq = self._session_load_seq

    def _bg():
        try:
            sessions = get_sessions()
            def _apply():
                if seq == self._session_load_seq:
                    self._panel.load_sessions(sessions)
                return False
            GLib.idle_add(_apply)
        except Exception as e:
            print(f"Error refreshing sessions after delete: {e}", flush=True)

    threading.Thread(target=_bg, daemon=True).start()

def _show_delete_summary(self, errs: list):
    """批次结束错误汇总（评审🟡7）：WARNING 框，≤3 条 + 手动清理指引。"""
    lines = "\n".join(f"• {e[:150]}" for e in errs[:3])
    if len(errs) > 3:
        lines += f"\n• … 等共 {len(errs)} 条"
    dialog = Gtk.MessageDialog(
        transient_for=None, modal=True,
        message_type=Gtk.MessageType.WARNING,
        buttons=Gtk.ButtonsType.OK,
        text="部分会话未能从数据库完全删除",
    )
    dialog.format_secondary_text(
        f"{lines}\n\n这些会话已从列表隐藏，但数据可能仍存在于数据库。\n"
        f"可手动执行：opencode session delete <session_id>")
    dialog.connect("response", lambda dlg, _: dlg.destroy())
    dialog.show_all()
```
- 注意：`_on_delete_session` 中原 `delete_session` 直调移除，改由 worker 执行

### 文件 4：`tests/test_session_store.py`（新增，约 65 行）

5 个用例（setUp patch `session_store.DB_PATH` 到 tempfile 临时库并建 session 表 + 种子行）：
1. **官方命令成功 → None + 物理删除**：mock `shutil.which` 返回路径 + `subprocess.run` 返回 `returncode=0`，且 `side_effect` 中同步执行临时库 `DELETE`（模拟 CLI 真实效果）→ 断言返回 None、`_session_exists` False、time_archived 为 NULL
2. **opencode 缺失 → 警告 + 软删兜底**：mock `which` 返回 None → 断言返回含"警告"、time_archived 非空
3. **命令非零 → 警告 + 软删兜底**：mock run 返回 `returncode=1, stderr="boom"` → 断言警告含 "boom"、time_archived 非空
4. **超时路径**：mock run 抛 `subprocess.TimeoutExpired` → 断言警告 + 兜底生效（评审🟡6 补充）
5. **CLI 成功但行仍在（交叉验证失败）**：mock run 返回 0 且不删行 → 断言走软删兜底（time_archived 非空）（评审🟡8）

### 文件 5：`tests/test_delete_queue.py`（新增，约 55 行）

`DeleteQueue` 纯逻辑单测（无 GTK 依赖）：
1. **串行消费顺序**：enqueue 3 个 → 断言按序处理
2. **批次边界**：worker 即将退出（短 IDLE_TIMEOUT 注入）时 enqueue → 断言不滞留、被处理（评审🟡3）
3. **异常自愈**：do_delete 第 1 条抛异常 → errors 收集、第 2 条仍处理、on_batch_done 收到汇总（评审🔴1）
4. **批次汇总**：多条错误 → on_batch_done 收到完整列表

## 四、验证标准

1. `venv/bin/python3 -m unittest discover tests`（150 + 新增 9）全绿
2. 手动测试清单：
   - 单个删除：确认框新文案 → 列表移除 → `opencode session list` 确认数据库已无
   - **连续删除 3-5 个**：全部移除、无 UI 卡顿、无锁错误（核心并发场景）
   - 删除同时打开/搜索面板：列表最终一致（seq 联动生效，不"诈尸"）
   - 回退场景：`mv $(which opencode) /tmp/` → 删除 → 列表隐藏 + WARNING 汇总框 + 恢复
   - live 会话删除：确认框提示，行为可接受

## 五、风险与对策

| 风险 | 对策 |
|------|------|
| opencode 命令失败/超时/输出乱码 | errors="replace" + try/except + 软删兜底 + 汇总提示 |
| 连续删除并发写库锁竞争 | DeleteQueue 单 worker 串行 |
| 快速连删刷新乱序/在途加载覆盖 | seq 联动（_session_load_seq 失效） |
| worker 异常死亡 | 循环内 try/except + finally 清理 |
| 测试污染真实 DB | patch DB_PATH 到 tempfile |

## 六、改动规模

- 代码文件 3 个：session_store.py（改）、delete_queue.py（新）、main.py（改）
- 测试文件 2 个：test_session_store.py、test_delete_queue.py
- 总约 260 行
