#!/usr/bin/python3
import os
import fcntl
import signal
import subprocess
import sys
import threading
import time
from typing import Optional
import gi
gi.require_version("Gtk", "3.0")
gi.require_version("AyatanaAppIndicator3", "0.1")
from gi.repository import Gtk, GLib, AyatanaAppIndicator3

from system.hotkey import HotkeyManager
from views.panel import SearchPanel
from stores.session_store import get_sessions, delete_session, rename_session
from stores.delete_queue import DeleteQueue
from stores.session_refresh import run_rename_refresh
from system.launcher import launch_session, launch_new_session, launch_session_pure
# ponytail: removed PromptStore import
from stores.clipboard_store import ClipboardStore, CategoryStore
from views.clipboard_panel import ClipboardPanel
from dialogs.memory_manager_dialog import show_memory_manager_dialog

from stores.theme_config import load_theme_config, save_theme_config, CONFIG_DIR


class App:
    def __init__(self):
        self._theme = load_theme_config()
        try:
            from system.migrate_history import run_migration
            run_migration()
        except Exception as e:
            print(f"Failed to run history migration: {e}")
        self._clip_store = ClipboardStore()
        # ponytail: removed unused self._prompt_store = PromptStore()
        self._cat_store = CategoryStore()
        self._panel = SearchPanel()
        self._panel.set_theme(self._theme)

        clip_panel = ClipboardPanel(self._clip_store, self._cat_store)
        self._clip_panel = clip_panel
        clip_panel.on_copy_clipboard = self._on_clipboard_copied
        clip_panel.on_hide_request = lambda: GLib.idle_add(self._panel.hide)
        self._panel.set_clipboard_panel(clip_panel, self._clip_store, self._cat_store)

        self._hotkey = HotkeyManager()
        self._running = True
        self._restart_requested = False
        self._indicator = self._build_indicator()

        self._panel.on_select = self._on_session_selected
        self._panel.on_open = self._on_panel_opened
        self._panel.on_delete_session = self._on_delete_session
        self._panel.on_rename_session = self._on_rename_session
        self._panel.on_launch_pure = self._on_session_launch_pure
        self._hotkey.on_trigger = lambda: self._on_hotkey()
        self._hotkey.on_trigger_ai = lambda: self._on_hotkey_ai()

        # 并发安全删除队列：单 worker 串行消费，连续删除不阻塞 UI、不锁冲突
        self._delete_queue = DeleteQueue(
            do_delete=lambda sid: delete_session(sid),
            on_refresh=lambda: self._schedule_refresh_after_delete(),
            on_batch_done=lambda errs: GLib.idle_add(self._show_delete_summary, errs),
        )

        # SIGTERM（systemd stop）→ 通过主循环安全退出，触发 _shutdown_mcp 清理，
        # 避免 KillMode=process 下 MCP 子进程被孤立
        try:
            signal.signal(signal.SIGTERM, self._on_sigterm)
        except (ValueError, AttributeError):
            pass

    def _build_indicator(self):
        ind = AyatanaAppIndicator3.Indicator.new(
            "opencode-switcher",
            "utilities-terminal",
            AyatanaAppIndicator3.IndicatorCategory.APPLICATION_STATUS,
        )
        ind.set_status(AyatanaAppIndicator3.IndicatorStatus.ACTIVE)

        menu = Gtk.Menu.new()
        show_item = Gtk.MenuItem.new_with_label("Show / Hide")
        show_item.connect("activate", lambda *_: GLib.idle_add(self._panel.toggle))
        menu.append(show_item)

        theme_menu = Gtk.Menu.new()
        dark_item = Gtk.RadioMenuItem.new_with_label(None, "Dark (经典深色)")
        dark_moon_item = Gtk.RadioMenuItem.new_with_label_from_widget(dark_item, "Dark Moon (紫月星云)")
        light_item = Gtk.RadioMenuItem.new_with_label_from_widget(dark_item, "Light (浅色)")
        if self._theme == "light":
            light_item.set_active(True)
        elif self._theme == "dark-moon":
            dark_moon_item.set_active(True)
        else:
            dark_item.set_active(True)
        dark_item.connect("toggled", lambda item: self._on_theme_changed("dark") if item.get_active() else None)
        dark_moon_item.connect("toggled", lambda item: self._on_theme_changed("dark-moon") if item.get_active() else None)
        light_item.connect("toggled", lambda item: self._on_theme_changed("light") if item.get_active() else None)
        theme_menu.append(dark_item)
        theme_menu.append(dark_moon_item)
        theme_menu.append(light_item)
        theme_menu_item = Gtk.MenuItem.new_with_label("Theme")
        theme_menu_item.set_submenu(theme_menu)
        menu.append(theme_menu_item)

        memory_item = Gtk.MenuItem.new_with_label("🗄️ 管理记忆")
        memory_item.connect("activate", lambda *_: GLib.idle_add(
            show_memory_manager_dialog, None
        ))
        menu.append(memory_item)

        menu.append(Gtk.SeparatorMenuItem.new())
        restart_item = Gtk.MenuItem.new_with_label("Restart")
        restart_item.connect("activate", lambda *_: GLib.idle_add(self._on_restart))
        menu.append(restart_item)
        quit_item = Gtk.MenuItem.new_with_label("Quit")
        quit_item.connect("activate", lambda *_: GLib.idle_add(self._confirm_quit))
        menu.append(quit_item)
        menu.show_all()

        ind.set_menu(menu)
        return ind

    def _on_theme_changed(self, theme: str):
        self._theme = theme
        self._panel.set_theme(theme)
        from stores.theme_config import save_theme_config
        save_theme_config(theme)

    def _confirm_quit(self):
        dialog = Gtk.MessageDialog(
            transient_for=None,
            modal=True,
            message_type=Gtk.MessageType.QUESTION,
            buttons=Gtk.ButtonsType.YES_NO,
            text="Quit OpenCode Switcher?",
        )
        dialog.format_secondary_text("The app will stop and the hotkey will be unregistered.")
        dialog.connect("response", self._on_quit_response)
        dialog.show_all()

    def _on_quit_response(self, dialog, response):
        dialog.destroy()
        if response == Gtk.ResponseType.YES:
            self.stop()

    def _on_restart(self):
        self._restart_requested = True
        self.stop()

    def _on_clipboard_copied(self, text: str, item_hash: Optional[str] = None):
        self._clip_store.mark_written(text, item_hash)

    def _on_hotkey(self):
        GLib.idle_add(self._panel.toggle)

    def _on_hotkey_ai(self):
        GLib.idle_add(self._panel.toggle_ai)

    def run(self):
        # 冷启动预热：等 WebKit 进程/文档就绪后再注册热键，避免用户重启系统后
        # 首次打开 AI 面板时 WebView realize 撞上冷 spawn（~2s）阻塞主线程。
        # 超时自动放弃（best-effort），绝不无限阻塞启动。
        try:
            self._clip_panel.wait_ai_webview_ready(timeout=10.0)
        except Exception as e:
            print(f"[AI] wait webview ready failed: {e}", flush=True)
        # 等待期间泵主循环会分发托盘菜单事件：用户可能已请求退出/重启
        # （Gtk.main_quit 在 Gtk.main 进入前是 no-op），此处短路避免应用
        # "退出"后继续运行；_restart_requested 由 __main__ 重启流程接手。
        if not self._running or self._restart_requested:
            return
        self._hotkey.start()
        Gtk.main()

    def _on_sigterm(self, signum, frame):
        """SIGTERM 保守处理：仅调度主循环退出，不在信号上下文直接执行复杂清理。"""
        GLib.idle_add(self.stop)

    def _shutdown_mcp(self):
        """退出路径：关闭 MCP 连接并停止 asyncio 桥接器（幂等）。

        Ctrl+C / SIGTERM / Quit / Restart 路径仅调用 Gtk.main_quit()，
        不保证触发 widget destroy，因此需显式清理，避免孤立 MCP 子进程。
        """
        panel = getattr(self, "_clip_panel", None)
        if panel is None:
            return
        try:
            panel.shutdown_mcp()
        except Exception as e:
            print(f"opencode-switcher: MCP shutdown error: {e}", flush=True)

    def stop(self):
        self._running = False
        self._hotkey.stop()
        self._indicator.set_status(AyatanaAppIndicator3.IndicatorStatus.PASSIVE)
        self._indicator = None
        self._shutdown_mcp()
        Gtk.main_quit()

    def _on_panel_opened(self):
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

    def _on_session_selected(self, session):
        try:
            if session.id == "new-opencode":
                err = launch_new_session(session.directory)
            else:
                err = launch_session(session.id, session.directory)
            if err:
                print(f"opencode-switcher: {err}", flush=True)
                GLib.idle_add(self._show_error, err)
        except Exception as e:
            print(f"opencode-switcher: Crash: {e}", flush=True)
            import traceback
            traceback.print_exc()

    def _on_session_launch_pure(self, session):
        try:
            err = launch_session_pure(session.id, session.directory)
            if err:
                print(f"opencode-switcher: {err}", flush=True)
                GLib.idle_add(self._show_error, err)
        except Exception as e:
            print(f"opencode-switcher: Crash: {e}", flush=True)
            import traceback
            traceback.print_exc()

    def _show_error(self, msg: str):
        dialog = Gtk.MessageDialog(
            transient_for=None,
            modal=True,
            message_type=Gtk.MessageType.ERROR,
            buttons=Gtk.ButtonsType.OK,
            text="Failed to launch session",
        )
        dialog.format_secondary_text(msg)
        dialog.connect("response", lambda dlg, _: dlg.destroy())
        dialog.show_all()

    def _on_delete_session(self, session):
        """确认框 → 入队（worker 串行执行，不阻塞 UI）。"""
        def on_confirm(dialog, response):
            dialog.destroy()
            if response != Gtk.ResponseType.YES:
                # 取消时复位 panel 的删除保护标志（避免 60s 内面板不自动隐藏）
                self._panel.reset_delete_guard()
                return
            self._delete_queue.enqueue(session.id)
        dialog = Gtk.MessageDialog(
            transient_for=None,
            modal=True,
            message_type=Gtk.MessageType.QUESTION,
            buttons=Gtk.ButtonsType.YES_NO,
            text=f'Delete "{session.title}"?',
        )
        dialog.format_secondary_text(
            "This session will be permanently deleted from the database. "
            "If deletion fails, it will be archived and hidden from the list."
        )
        dialog.connect("response", on_confirm)
        dialog.show_all()

    def _schedule_refresh_after_delete(self):
        """删除后刷新：使在途后台加载失效（防"诈尸"）+ 后台线程加载（不卡 UI）。

        注：本方法在 worker 线程被调用，与主线程 _on_panel_opened 并发写
        _session_load_seq。CPython GIL 保证整数自增原子性，最坏情况仅某次
        刷新因 seq 不匹配被跳过，无害。
        """
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

    def _show_delete_summary(self, errs):
        """批次结束错误汇总：WARNING 框，≤3 条 + 手动清理指引。"""
        lines = "\n".join(f"• {e[:150]}" for e in errs[:3])
        if len(errs) > 3:
            lines += f"\n• … 等共 {len(errs)} 条"
        dialog = Gtk.MessageDialog(
            transient_for=None,
            modal=True,
            message_type=Gtk.MessageType.WARNING,
            buttons=Gtk.ButtonsType.OK,
            text="部分会话未能从数据库完全删除",
        )
        dialog.format_secondary_text(
            f"{lines}\n\n这些会话已从列表隐藏，但数据可能仍存在于数据库。\n"
            f"可手动执行：opencode session delete <session_id>"
        )
        dialog.connect("response", lambda dlg, _: dlg.destroy())
        dialog.show_all()

    def _on_rename_session(self, session_id: str, new_title: str):
        """重命名：DB 写入 + 列表刷新全部移入后台线程，不阻塞 GTK 主线程。

        先递增 _session_load_seq 使在途后台加载失效（防旧标题覆盖新列表），
        再派发后台线程执行 rename_session + get_sessions（含 pgrep /proc
        扫描，代价高）。UI 更新经 run_rename_refresh 内部 GLib.idle_add
        切回主线程，apply 前校验 seq，防止乱序/快速连改覆盖
        （与 _schedule_refresh_after_delete 同一套约定）。
        """
        self._session_load_seq = getattr(self, "_session_load_seq", 0) + 1
        seq = self._session_load_seq

        threading.Thread(
            target=run_rename_refresh,
            kwargs={
                "session_id": session_id,
                "new_title": new_title,
                "rename_fn": rename_session,
                "load_fn": get_sessions,
                "seq": seq,
                "get_seq": lambda: self._session_load_seq,
                "schedule": GLib.idle_add,
                "apply_fn": self._panel.load_sessions,
                "show_error_fn": self._show_error,
                "log_error": lambda msg: print(msg, flush=True),
            },
            daemon=True,
        ).start()


if __name__ == "__main__":
    import traceback
    from stores.theme_config import CONFIG_DIR

    # Single-instance lock
    LOCK_PATH = os.path.join(CONFIG_DIR, "lock")
    os.makedirs(CONFIG_DIR, exist_ok=True)
    try:
        lock_fd = open(LOCK_PATH, "w")
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (IOError, OSError):
        print("opencode-switcher: another instance is already running", flush=True)
        sys.exit(0)

    # Startup permission sweep: tighten known sensitive files to 0o600 and
    # private data dirs to 0o700 (legacy 0644 clipboard/conversation/memory/
    # todo files). Best-effort only — a failure must never block startup.
    try:
        from system.utils import sweep_sensitive_permissions
        sweep_sensitive_permissions()
    except Exception as e:
        print(f"opencode-switcher: permission sweep failed: {e}", flush=True)

    # 冷启动预热：后台预读 WebKit 共享库进 page cache（与 App 初始化并行），
    # 降低重启系统后首次打开 AI 对话框时 WebProcess 冷 spawn 的 1-2s 卡顿。
    try:
        from system.utils import preload_webkit_libs
        threading.Thread(target=preload_webkit_libs, daemon=True).start()
    except Exception:
        pass  # best-effort，绝不阻塞启动

    # AI 渲染依赖预热：markdown/pygments/tiktoken 首次加载在冷启动可达数秒，
    # 后台预热避免首次打开 AI 面板时主线程渲染阻塞。
    try:
        def _prewarm_ai_render():
            from ai_text_utils.markdown import _markdown_to_html_safe
            _markdown_to_html_safe(
                "**warm**\n```python\nprint(1)\n```\n```bash\necho hi\n```\n```json\n{}\n```",
                fallback_content="",
            )
            try:
                import tiktoken
                tiktoken.get_encoding("cl100k_base").encode("warmup")
            except Exception:
                pass  # 无 tiktoken 时走字符启发式回退
        threading.Thread(target=_prewarm_ai_render, daemon=True).start()
    except Exception:
        pass  # best-effort，绝不阻塞启动

    app = App()
    try:
        app.run()
    except KeyboardInterrupt:
        app.stop()
    except Exception:
        traceback.print_exc()
        sys.exit(1)

    if app._restart_requested:
        lock_fd.close()
        env = os.environ.copy()
        env["JSC_useJIT"] = "false"
        subprocess.Popen([sys.executable] + sys.argv, env=env, stderr=subprocess.DEVNULL)
