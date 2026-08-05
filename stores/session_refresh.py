"""重命名会话的后台刷新调度（纯逻辑，无 GTK 依赖，可单测）。

与 main.py 删除刷新（_schedule_refresh_after_delete）同一套约定：
- 调用方（GTK 线程）先递增 _session_load_seq，使在途后台加载失效
  （防止面板打开时的旧加载覆盖重命名后的新列表，即"旧标题诈尸"）；
- 本模块函数在后台线程执行 DB 写入与读取（rename_session / get_sessions
  含 pgrep /proc 扫描，代价高，不能在 GTK 线程同步执行）；
- UI 更新通过 schedule() 回调切回主线程（生产为 GLib.idle_add），
  apply 前再次校验 seq 仍为当前值，防止乱序/快速连改覆盖；
- 失败路径：DB 错误走 show_error_fn 弹框（与原同步行为一致）；
  异常路径走 log_error 打印（与 _on_panel_opened 的 _bg_load 约定一致）。
"""
from typing import Callable, List, Optional


def run_rename_refresh(
    session_id: str,
    new_title: str,
    *,
    rename_fn: Callable[[str, str], Optional[str]],
    load_fn: Callable[[], List],
    seq: int,
    get_seq: Callable[[], int],
    schedule: Callable[[Callable], None],
    apply_fn: Callable[[List], None],
    show_error_fn: Callable[[str], None],
    log_error: Callable[[str], None],
) -> None:
    """后台线程入口：rename → 失败 schedule 错误；成功 → 加载并 seq 校验后 apply。

    参数：
    - rename_fn(sid, title) -> None/err：DB 重命名（成功后内部已失效会话缓存）
    - load_fn() -> sessions：重命名后的列表读取（TTL 缓存内会复用）
    - seq / get_seq()：本次刷新的序列号与当前序列号读取器，
      apply 时 seq != get_seq() 则丢弃（在途/乱序刷新不落地）
    - schedule(cb)：切回 UI 线程调度（生产 GLib.idle_add）
    - apply_fn(sessions)：UI 线程落地（panel.load_sessions）
    - show_error_fn(err)：DB 返回错误时的用户可见提示
    - log_error(msg)：异常兜底日志（后台线程异常不杀死应用）
    """
    try:
        err = rename_fn(session_id, new_title)
    except Exception as e:
        # 与 _on_panel_opened 的 _bg_load 一致：后台异常打印日志，不弹框
        log_error(f"Error renaming session: {e}")
        return

    if err:
        # 与原有行为兼容：错误经 idle 切回主线程弹 _show_error 框
        schedule(lambda: show_error_fn(err))
        return

    try:
        sessions = load_fn()
    except Exception as e:
        log_error(f"Error refreshing sessions after rename: {e}")
        return

    def _apply():
        # seq 校验：仅当前序列号的刷新落地，防乱序/在途加载覆盖
        if seq == get_seq():
            apply_fn(sessions)
        return False  # GLib.idle_add 一次性回调约定

    schedule(_apply)
